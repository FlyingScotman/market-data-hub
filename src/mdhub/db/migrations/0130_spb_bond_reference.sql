-- SPB Russian bonds are independent of the MOEX reference and snapshot universe.
INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
('spb', 'SPB Exchange public website', 'https://spbexchange.ru',
 'Public website data; exchange terms apply. Chart close is not official close.');

CREATE TABLE ref.spb_bond (
    venue text NOT NULL DEFAULT 'SPB' CHECK (venue = 'SPB'),
    isin text NOT NULL CHECK (isin ~ '^RU[A-Z0-9]{9}[0-9]$'),
    ticker text NOT NULL,
    security_id bigint NOT NULL CHECK (security_id > 0),
    name text NOT NULL,
    price_unit text NOT NULL CHECK (price_unit = 'PCT' OR price_unit ~ '^[A-Z]{3}$'),
    settlement_currency text NOT NULL CHECK (settlement_currency ~ '^[A-Z]{3}$'),
    card_price_unit text NOT NULL,
    unit_conflict boolean NOT NULL,
    unit_reference_date date NOT NULL,
    unit_capture_id bigint REFERENCES ops.raw_capture(id),
    capture_id bigint REFERENCES ops.raw_capture(id),
    run_id bigint,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz,
    PRIMARY KEY (venue, isin),
    UNIQUE (venue, ticker),
    UNIQUE (venue, security_id)
);
CREATE INDEX spb_bond_capture_idx ON ref.spb_bond(capture_id);
CREATE INDEX spb_bond_unit_capture_idx ON ref.spb_bond(unit_capture_id);
CALL ops.enable_history('ref.spb_bond', ARRAY['venue', 'isin'], ARRAY['unit_capture_id']);
