-- As-known-at reader for the manual redemption overrides (0122), read by the static snapshot
-- builder (contract market_snapshot/1.10: Redemption source / override).
CALL ops.create_known_at_function('ref.bond_amortization_override_known_at',
                                  'ref.bond_amortization_override',
                                  ARRAY['secid', 'amort_date']);
