-- As-known-at readers for the agency ratings (0120), read by the static snapshot builder
-- (contract market_snapshot/1.3: issuer-scope ratings).
CALL ops.create_known_at_function('ref.issuer_rating_known_at', 'ref.issuer_rating',
                                  ARRAY['issuer_id', 'agency', 'scale', 'source', 'rating_type']);
-- re-created so its row type carries the ref.bond_rating columns added by 0120
CALL ops.create_known_at_function('ref.bond_rating_known_at', 'ref.bond_rating',
                                  ARRAY['secid', 'agency', 'scale', 'source']);
