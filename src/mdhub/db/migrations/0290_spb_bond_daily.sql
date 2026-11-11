CREATE TABLE md.spb_bond_daily (
    venue text NOT NULL DEFAULT 'SPB' CHECK (venue = 'SPB'),
    isin text NOT NULL,
    trade_date date NOT NULL,
    ticker text NOT NULL,
    source text NOT NULL DEFAULT 'spb' REFERENCES ops.source(code) CHECK (source = 'spb'),
    price_basis text NOT NULL DEFAULT 'chart_close' CHECK (price_basis = 'chart_close'),
    price_unit text NOT NULL,
    unit_reference_date date NOT NULL,
    card_price_unit text NOT NULL,
    unit_conflict boolean NOT NULL,
    open numeric NOT NULL CHECK (open > 0 AND open <> 'NaN'::numeric),
    high numeric NOT NULL CHECK (high > 0 AND high <> 'NaN'::numeric),
    low numeric NOT NULL CHECK (low > 0 AND low <> 'NaN'::numeric),
    close numeric NOT NULL CHECK (close > 0 AND close <> 'NaN'::numeric),
    capture_id bigint REFERENCES ops.raw_capture(id),
    run_id bigint,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz,
    PRIMARY KEY (venue, isin, trade_date),
    FOREIGN KEY (venue, isin) REFERENCES ref.spb_bond(venue, isin),
    CHECK (low <= open AND open <= high AND low <= close AND close <= high)
);
CREATE INDEX spb_bond_daily_capture_idx ON md.spb_bond_daily(capture_id);
CREATE INDEX spb_bond_daily_date_idx ON md.spb_bond_daily(trade_date);
CALL ops.enable_history('md.spb_bond_daily', ARRAY['venue', 'isin', 'trade_date']);
COMMENT ON TABLE md.spb_bond_daily IS
 'SPB chart daily OHLC, chart_close basis, never official closing price. No forward fill. '
 'UTC daily bucket date, loaded only before the current Moscow date. PCT = percent of par; '
 'other price_unit values are currency amounts. Unit reference is the observed official report '
 'at ingestion, not a historically known reference. run_id joins the captured unit report/card.';
