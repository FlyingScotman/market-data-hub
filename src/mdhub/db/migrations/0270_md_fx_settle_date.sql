-- 0270_md_fx_settle_date: value dates of MOEX currency-market instruments per trade date
-- (contract 1.4, TDM brief H13, package H2). ISS publishes SETTLEDATE only in the securities
-- listing of the current session (ref.fx_instrument.settle_date keeps the latest value, without
-- history); the selt / CETS listings are now read with their dataversion block and every
-- (session date, secid, board) -> SETTLEDATE is stored here. Swaps carry the near-leg date.
-- Loader: mdhub.loaders.fx_moex (fx_moex_eod, fx_moex_intraday). Never synthesized: a session
-- whose listing was not read has no row.

CREATE TABLE md.fx_settle_date (
    trade_date   date NOT NULL,              -- dataversion.trade_date of the listing (session)
    secid        text NOT NULL,
    board        text NOT NULL,
    settle_date  date NOT NULL,              -- SETTLEDATE as published for that session
    run_id       bigint,
    capture_id   bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,  -- intraday captures
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, secid, board)
);
CREATE INDEX fx_settle_date_capture_idx ON md.fx_settle_date (capture_id);
COMMENT ON TABLE md.fx_settle_date IS
    'MOEX selt/CETS SETTLEDATE per session (dataversion.trade_date) and instrument: the value date '
    'of an outright, the near-leg value date of a swap. Read by the market snapshot (fx_rates '
    'settle_date, fx_swaps near_settle_date / far_settle_date)';
CALL ops.enable_history('md.fx_settle_date', ARRAY['trade_date', 'secid', 'board']);
