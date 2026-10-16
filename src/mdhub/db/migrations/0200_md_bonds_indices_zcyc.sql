-- 0200_md_bonds_indices_zcyc: MOEX market data from ISS (DESIGN §4 "md"):
--   md.bond_eod, md.bond_eod_yield     bonds EOD history by date, all boards of market "bonds"
--   md.bond_quote_intraday (+ _last)   bonds marketdata snapshots, changed rows only
--   md.index_eod                       all ISS indices EOD (RUSFAR family, RGBI, RUCBI, IMOEX...)
--   md.zcyc_snapshot (+ zcyc_reject)   MOEX KBD (G-curve) parameters + standard yields
-- Loaders: mdhub.loaders.bonds_eod / bonds_intraday / indices / zcyc; parsers in
-- mdhub.sources.moex_market. No FK to ref.bond (owned elsewhere): join by secid.

-- ------------------------------------------------------------------------- md.bond_eod
CREATE TABLE md.bond_eod (
    trade_date                 date NOT NULL,
    secid                      text NOT NULL,
    board                      text NOT NULL,
    short_name                 text,
    open                       numeric,
    low                        numeric,
    high                       numeric,
    close                      numeric,
    legal_close                numeric,
    waprice                    numeric,
    market_price2              numeric,
    market_price3              numeric,
    admitted_quote             numeric,
    mp2_val_trd                numeric,
    market_price3_trades_value numeric,
    admitted_value             numeric,
    accrued_int                numeric,
    face_value                 numeric,
    face_unit                  text,
    currency_id                text,
    volume                     numeric,
    value                      numeric,
    num_trades                 integer,
    yield_close_pct            numeric,
    yield_at_waprice_pct       numeric,
    yield_to_offer_pct         numeric,
    yield_last_coupon_pct      numeric,
    duration_days              numeric,
    coupon_pct                 numeric,
    coupon_value               numeric,
    mat_date                   date,
    offer_date                 date,
    buyback_date               date,
    call_option_date           date,
    call_option_yield_pct      numeric,
    call_option_duration_days  numeric,
    put_option_date            date,
    date_yield_from_issuer     date,
    last_trade_date            date,
    zspread_bp                 numeric,
    zspread_at_waprice_bp      numeric,
    iricpi_close               numeric,
    bei_close                  numeric,
    cbr_close                  numeric,
    trading_session            text,
    bond_type                  text,
    bond_subtype               text,
    capture_id                 bigint REFERENCES ops.raw_capture (id),
    run_id                     bigint,
    ingested_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz,
    PRIMARY KEY (trade_date, secid, board)
);
CREATE INDEX bond_eod_secid_idx ON md.bond_eod (secid, trade_date);
COMMENT ON TABLE md.bond_eod IS
    'MOEX ISS /history/engines/stock/markets/bonds/securities.json?date=, every board (one row per board, '
    'the legacy loader dropped multi-board rows). Prices in % of face (as ISS), value/accrued in currency_id';
COMMENT ON COLUMN md.bond_eod.value IS 'Turnover (VALUE) in the settlement currency currency_id (SUR = RUB)';
COMMENT ON COLUMN md.bond_eod.accrued_int IS 'ACCINT per bond in the settlement currency currency_id';
COMMENT ON COLUMN md.bond_eod.duration_days IS 'MOEX DURATION (days)';
CALL ops.enable_history('md.bond_eod', ARRAY['trade_date', 'secid', 'board']);

-- ------------------------------------------------------------------- md.bond_eod_yield
-- Sibling of md.bond_eod (same key) from /history/engines/stock/markets/bonds/yields.json?date=.
-- Kept separate because it is a different capture (capture_id is per table) and ISS lists
-- yields rows for boards without trades; rows with no values at all are not stored.
CREATE TABLE md.bond_eod_yield (
    trade_date                  date NOT NULL,
    secid                       text NOT NULL,
    board                       text NOT NULL,
    yield_date                  date,
    yield_date_type             text,
    price                       numeric,
    accrued_int                 numeric,
    zcyc_moment                 timestamptz,
    effective_yield_pct         numeric,
    duration_days               numeric,
    zspread_bp                  numeric,
    gspread_bp                  numeric,
    waprice                     numeric,
    effective_yield_waprice_pct numeric,
    duration_waprice_days       numeric,
    ir                          numeric,
    icpi                        numeric,
    bei                         numeric,
    cbr                         numeric,
    yield_to_offer_pct          numeric,
    yield_last_coupon_pct       numeric,
    trade_moment                date,
    capture_id                  bigint REFERENCES ops.raw_capture (id),
    run_id                      bigint,
    ingested_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz,
    PRIMARY KEY (trade_date, secid, board)
);
CREATE INDEX bond_eod_yield_secid_idx ON md.bond_eod_yield (secid, trade_date);
COMMENT ON TABLE md.bond_eod_yield IS
    'MOEX ISS bonds EOD yields (effective yield to yield_date of yield_date_type MATDATE/OFFERDATE/..., '
    'duration, Z/G-spreads vs KBD at zcyc_moment). Join md.bond_eod on (trade_date, secid, board)';
CALL ops.enable_history('md.bond_eod_yield', ARRAY['trade_date', 'secid', 'board']);

-- ------------------------------------------------------------- md.bond_quote_intraday
CREATE TABLE md.bond_quote_intraday (
    secid                text NOT NULL,
    board                text NOT NULL,
    ts                   timestamptz NOT NULL,
    source_ts            timestamptz NOT NULL,
    latency              text NOT NULL CHECK (latency IN ('realtime', 'delayed')),
    bid                  numeric,
    offer                numeric,
    last                 numeric,
    open                 numeric,
    low                  numeric,
    high                 numeric,
    waprice              numeric,
    market_price         numeric,
    market_price_today   numeric,
    lcurrent_price       numeric,
    close_price          numeric,
    yield_pct            numeric,
    yield_at_waprice_pct numeric,
    yield_to_offer_pct   numeric,
    duration_days        numeric,
    num_trades           integer,
    vol_today            numeric,
    val_today            numeric,
    val_today_rub        numeric,
    num_bids             integer,
    num_offers           integer,
    trading_status       text,
    trading_session      text,
    zspread_bp           numeric,
    effective_yield_pct  numeric,
    yield_date           date,
    yield_date_type      text,
    gspread_bp           numeric,
    capture_id           bigint REFERENCES ops.raw_capture (id),
    run_id               bigint,
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz,
    PRIMARY KEY (secid, board, ts)
);
CREATE INDEX bond_quote_intraday_ts_idx ON md.bond_quote_intraday (ts);
COMMENT ON TABLE md.bond_quote_intraday IS
    'MOEX ISS /engines/stock/markets/bonds/securities.json snapshots (all boards); a row is stored only '
    'when quote fields changed vs md.bond_quote_last. ts = UPDATETIME (MSK) of the quote, source_ts = SYSTIME';
COMMENT ON COLUMN md.bond_quote_intraday.latency IS
    'delayed when the freshest quote of the snapshot lags SYSTIME by > 120 s (public ISS: ~900 s)';
CALL ops.enable_history('md.bond_quote_intraday', ARRAY['secid', 'board', 'ts']);

CREATE TABLE md.bond_quote_last (LIKE md.bond_quote_intraday INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
ALTER TABLE md.bond_quote_last ADD PRIMARY KEY (secid, board);
ALTER TABLE md.bond_quote_last ADD FOREIGN KEY (capture_id) REFERENCES ops.raw_capture (id);
COMMENT ON TABLE md.bond_quote_last IS
    'Latest stored state per (secid, board) of md.bond_quote_intraday (change detection; no history)';

-- ------------------------------------------------------------------------ md.index_eod
CREATE TABLE md.index_eod (
    trade_date      date NOT NULL,
    secid           text NOT NULL,
    board           text,
    short_name      text,
    open            numeric,
    high            numeric,
    low             numeric,
    close           numeric,
    value           numeric,
    volume          numeric,
    duration_days   numeric,
    yield_pct       numeric,
    capitalization  numeric,
    currency_id     text,
    trading_session text,
    basis           text NOT NULL CHECK (basis IN ('history', 'marketdata')),
    source_ts       timestamptz,
    capture_id      bigint REFERENCES ops.raw_capture (id),
    run_id          bigint,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz,
    PRIMARY KEY (trade_date, secid)
);
CREATE INDEX index_eod_secid_idx ON md.index_eod (secid, trade_date);
COMMENT ON TABLE md.index_eod IS
    'All MOEX ISS indices by trade date (history/engines/stock/markets/index/securities.json?date=). '
    'basis=marketdata: same-day preliminary value from current marketdata (e.g. RUSFAR after the 12:30 '
    'fixing); replaced by basis=history when the history appears (old image kept in ops.row_history)';
COMMENT ON COLUMN md.index_eod.close IS 'Index value; for RUSFAR* the fixing in % p.a.';
CALL ops.enable_history('md.index_eod', ARRAY['trade_date', 'secid']);

-- --------------------------------------------------------------------- md.zcyc_snapshot
CREATE TABLE md.zcyc_snapshot (
    trade_date         date NOT NULL,
    trade_time         time NOT NULL,
    source_ts          timestamptz NOT NULL,
    params             jsonb NOT NULL,
    standard_yields    jsonb NOT NULL,
    check_max_error_bp numeric NOT NULL,
    check_tolerance_bp numeric NOT NULL,
    is_eod             boolean NOT NULL DEFAULT false,
    capture_id         bigint REFERENCES ops.raw_capture (id),
    run_id             bigint,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz,
    PRIMARY KEY (trade_date, trade_time),
    CONSTRAINT zcyc_snapshot_check_passed CHECK (check_max_error_bp < check_tolerance_bp),
    CONSTRAINT zcyc_snapshot_params_object CHECK (jsonb_typeof(params) = 'object'),
    CONSTRAINT zcyc_snapshot_yields_array CHECK (jsonb_typeof(standard_yields) = 'array')
);
CREATE UNIQUE INDEX zcyc_snapshot_one_eod_idx ON md.zcyc_snapshot (trade_date) WHERE is_eod;
COMMENT ON TABLE md.zcyc_snapshot IS
    'MOEX KBD (zero-coupon yield curve) publications from /iss/engines/stock/zcyc.json: params {B1,B2,B3,T1,G1..G9} '
    '(G in bp), standard_yields [{tenor_years, yield_pct}] (effective annual %). Only snapshots whose '
    'standard yields are reproduced from params within check_tolerance_bp are stored (failures: md.zcyc_reject)';
COMMENT ON COLUMN md.zcyc_snapshot.is_eod IS
    'Last publication of the day as returned by zcyc.json?date= (one per trade_date)';
CALL ops.enable_history('md.zcyc_snapshot', ARRAY['trade_date', 'trade_time'], ARRAY['is_eod']);

CREATE TABLE md.zcyc_reject (
    capture_id   bigint PRIMARY KEY REFERENCES ops.raw_capture (id),
    trade_date   date,
    trade_time   time,
    max_error_bp numeric,
    reason       text NOT NULL,
    run_id       bigint,
    rejected_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE md.zcyc_reject IS 'ZCYC payloads that failed validation (raw kept in ops.raw_capture)';
