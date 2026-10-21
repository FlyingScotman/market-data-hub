-- As-known-at reader for the curated issue terms (0121), read by the static snapshot builder
-- (contract market_snapshot/1.3: replacement_bond with basis document).
CALL ops.create_known_at_function('ref.bond_curated_term_known_at', 'ref.bond_curated_term',
                                  ARRAY['secid', 'term', 'term_key']);
