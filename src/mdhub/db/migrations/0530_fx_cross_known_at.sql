-- 0530_fx_cross_known_at: as-known-at reader of md.fx_cross (0260) for the market snapshot
-- cross_rates (contract 1.3) and /v1/history/fx?known_at=. Same rule as the 0500 readers.

CALL ops.create_known_at_function('md.fx_cross_known_at', 'md.fx_cross',
                                  ARRAY['rate_date', 'base_ccy', 'quote_ccy', 'kind'],
                                  ARRAY['ingested_at'], 'rate_date');
