CALL ops.create_known_at_function('ref.spb_bond_known_at', 'ref.spb_bond',
                                  ARRAY['venue', 'isin'], ARRAY['updated_at', 'ingested_at']);
CALL ops.create_known_at_function('md.spb_bond_daily_known_at', 'md.spb_bond_daily',
                                  ARRAY['venue', 'isin', 'trade_date'],
                                  ARRAY['updated_at', 'ingested_at'], 'trade_date');
