-- 0260_md_fx_cross_rates_sources: sources of the international reference rates (WEB-16) and the
-- non-RUB cross-rate table that backs up the ECB reference rates.
--
-- Sources (one code per host, so the source probe and ops.v_source_status tell them apart):
--   ecb_data_portal  data-api.ecb.europa.eu (SDMX): EUR short-term rate (EST), reference rates
--                    (EXR, fallback of www.ecb.europa.eu eurofxref), AAA euro area yield curve (YC)
--   bundesbank       api.statistiken.bundesbank.de (SDMX): mirror of the ECB EUR short-term rate,
--                    German government par yield curve (fallback proxy of the AAA curve)
--   dbnomics         api.db.nomics.world: mirror of the ECB EUR short-term rate (no key)
--   fred             fred.stlouisfed.org (fredgraph.csv, no key): SOFR, EUR short-term rate,
--                    H.10 noon buying rates (cross rates)
--   nyfed            markets.newyorkfed.org: SOFR, SOFR averages and index (administrator)
--   us_treasury      home.treasury.gov: daily par yield curve (proxy, not an OIS curve)
--
-- md.fx_cross: non-RUB cross rates other than the ECB reference rates (md.ecb_fx). Every kind is
-- its own series (never merged, DESIGN §2): cbr_cross (two CBR official rates, by effective
-- date), moex_indicative_cross (two MOEX 19:00 indicative rates), moex_cets (a traded CETS cross,
-- close per unit), fred_h10 (Federal Reserve H.10 noon buying rate via FRED).

INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('ecb_data_portal', 'ECB Data Portal', 'https://data-api.ecb.europa.eu',
     'Public; reuse with attribution to the ECB'),
    ('bundesbank', 'Deutsche Bundesbank time series', 'https://api.statistiken.bundesbank.de',
     'Public; reuse with attribution to the Deutsche Bundesbank'),
    ('dbnomics', 'DBnomics (CEPREMAP)', 'https://api.db.nomics.world',
     'Public mirror of provider data; the provider''s (ECB) terms apply'),
    ('fred', 'FRED (Federal Reserve Bank of St. Louis)', 'https://fred.stlouisfed.org',
     'Public; series owners'' terms apply (ECB, Federal Reserve)'),
    ('nyfed', 'Federal Reserve Bank of New York Markets API', 'https://markets.newyorkfed.org',
     'Public; SOFR terms of use of the NY Fed, attribution'),
    ('us_treasury', 'U.S. Department of the Treasury', 'https://home.treasury.gov',
     'Public domain (U.S. government work)')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE md.fx_cross (
    rate_date   date NOT NULL,
    base_ccy    text NOT NULL CHECK (base_ccy ~ '^[A-Z]{3}$'),
    quote_ccy   text NOT NULL CHECK (quote_ccy ~ '^[A-Z]{3}$' AND quote_ccy <> base_ccy),
    kind        text NOT NULL
                CHECK (kind IN ('cbr_cross', 'moex_indicative_cross', 'moex_cets', 'fred_h10')),
    source      text NOT NULL REFERENCES ops.source (code),
    rate        numeric NOT NULL CHECK (rate > 0),
    details     jsonb,
    capture_id  bigint REFERENCES ops.raw_capture (id),
    run_id      bigint,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, base_ccy, quote_ccy, kind),
    CONSTRAINT fx_cross_details_object CHECK (details IS NULL OR jsonb_typeof(details) = 'object')
);
CREATE INDEX fx_cross_pair_idx ON md.fx_cross (base_ccy, quote_ccy, kind, rate_date);
CREATE INDEX fx_cross_capture_idx ON md.fx_cross (capture_id);
COMMENT ON TABLE md.fx_cross IS
    'Non-RUB cross rates besides the ECB reference rates (md.ecb_fx): rate = units of quote_ccy '
    'per 1 base_ccy. kind cbr_cross: CBR official RUB rates of both currencies in force on '
    'rate_date (effective date), divided; moex_indicative_cross: MOEX 19:00 (mc) indicative RUB '
    'rates of the trade date, divided; moex_cets: CETS cross outright (TOM) close per unit, only '
    'days with trades; fred_h10: Federal Reserve H.10 noon buying rate in New York (via FRED). '
    'Derived kinds are rounded to 10 significant digits; details name the inputs.';
COMMENT ON COLUMN md.fx_cross.details IS
    'Inputs as strings (derived kinds: both RUB rates and their dates; fred_h10: series id)';
CALL ops.enable_history('md.fx_cross', ARRAY['rate_date', 'base_ccy', 'quote_ccy', 'kind']);

-- mdhub_api reads md schema-wide; the grant procedure (0800) re-grants it on existing databases.
DO $$
BEGIN
    IF to_regprocedure('admin.grant_service_roles()') IS NOT NULL THEN
        CALL admin.grant_service_roles();
    END IF;
END
$$;
