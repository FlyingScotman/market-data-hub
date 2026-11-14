-- Replay the action versions actually observed by the hub at an instant. This does not
-- establish the completeness of an agency's action history or a licence to distribute it.
CALL ops.create_known_at_function(
    'ref.rating_action_known_at', 'ref.rating_action',
    ARRAY['agency', 'object_kind', 'object_key', 'rating_type', 'scale', 'action_date', 'action_ref'],
    ARRAY['ingested_at'], 'action_date'
);

CREATE INDEX rating_action_object_date_idx
    ON ref.rating_action (object_kind, object_key, action_date);
