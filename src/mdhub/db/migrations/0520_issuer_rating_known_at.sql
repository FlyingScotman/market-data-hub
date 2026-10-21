-- As-known-at readers for the issuer reference (0110) and ratings (WEB-13, HS1/HS4/HS5).
-- ref.issuer_known_at is re-created so its row type carries the columns added by 0110.
CALL ops.create_known_at_function('ref.issuer_known_at', 'ref.issuer', ARRAY['issuer_id']);
CALL ops.create_known_at_function('ref.issuer_card_known_at', 'ref.issuer_card', ARRAY['issuer_id']);
CALL ops.create_known_at_function('ref.bond_rating_known_at', 'ref.bond_rating',
                                  ARRAY['secid', 'agency', 'scale', 'source']);
