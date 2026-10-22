-- 0550_derivative_inputs_known_at: as-known-at readers of the derivative inputs the market
-- snapshot reads from contract 1.4 (package P6): FX value dates (md.fx_settle_date), FORTS
-- futures EOD (md.futures_eod) and contract static (ref.futures_contract).
CALL ops.create_known_at_function('md.fx_settle_date_known_at', 'md.fx_settle_date',
                                  ARRAY['trade_date', 'secid', 'board'],
                                  ARRAY['ingested_at'], 'trade_date');
CALL ops.create_known_at_function('md.futures_eod_known_at', 'md.futures_eod',
                                  ARRAY['trade_date', 'secid'],
                                  ARRAY['ingested_at'], 'trade_date');
CALL ops.create_known_at_function('ref.futures_contract_known_at', 'ref.futures_contract',
                                  ARRAY['secid']);
