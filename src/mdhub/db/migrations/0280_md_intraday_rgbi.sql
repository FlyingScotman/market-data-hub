-- 0280_md_intraday_rgbi: intraday inputs of the RGBI futures fair-value monitor.
-- Owner: loaders futures.py (futures_intraday: RGBI/RGBIF/KEYRATE/RUONIA contracts and their
-- trades), zcyc_securities.py, index_intraday.py, repo.py, bond_close.py.
--   md.futures_intraday   + bid_depth, offer_depth (best-level volumes when the feed has them)
--   md.futures_trade      FORTS trades of the rates contracts (exchange time, price, volume, side, OI)
--   md.zcyc_security_quote  ISS /engines/stock/zcyc.json block securities (KBD bid/ask per OFZ)
--   md.index_intraday     index marketdata (RGBI, RGBITR: value, high/low, change)
--   md.index_candle       index candles (1-minute) incl. backfill
--   md.index_constituent  index composition and weights (RGBI and sub-indices), daily
--   md.repo_intraday      repo with the CCP: per-bond (market ccp: EQRP/EQWP/EQMP/EQTP) and GC
--                         (market gcc: GCRP, GCOW, ...) marketdata snapshots, changed rows only
--   md.repo_eod           daily history of the same boards
--   md.bond_close_quote   closing bid/ask of bonds at the close cut (default 18:45 MSK)
-- Every intraday row carries the exchange clock (source_ts), ingested_at and latency
-- (realtime / delayed). capture_id of tables fed by "intraday:*" captures is ON DELETE SET NULL
-- (raw retention). Plain tables with (key, ts) indexes; no TimescaleDB (not in the stack).

-- ------------------------------------------------------------ md.futures_intraday (extend)
ALTER TABLE md.futures_intraday
    ADD COLUMN bid_depth   numeric,
    ADD COLUMN offer_depth numeric;
COMMENT ON COLUMN md.futures_intraday.bid_depth IS
    'BIDDEPTH: contracts at the best bid (null when the feed does not publish it, e.g. public ISS)';
COMMENT ON COLUMN md.futures_intraday.offer_depth IS
    'OFFERDEPTH: contracts at the best offer (null when the feed does not publish it)';
COMMENT ON TABLE md.futures_intraday IS
    'FORTS futures intraday (FX contracts and the rates contracts of MDHUB_FUTURES_RATES_ASSETS: RGBI, RGBIF, KEYRATE, RUONIA): '
    'APIM realtime (moex_apim/apim_marketdata), fallback public ISS (moex_iss/iss_public, delayed); change-only per (secid, source, source_variant)';
CREATE INDEX futures_intraday_secid_ts_idx ON md.futures_intraday (secid, ts);

-- ------------------------------------------------------------------- md.futures_trade
CREATE TABLE md.futures_trade (
    secid          text NOT NULL,
    trade_no       bigint NOT NULL,           -- TRADENO
    source         text NOT NULL REFERENCES ops.source (code),  -- moex_apim / moex_iss
    trade_ts       timestamptz NOT NULL,      -- TRADEDATE + TRADETIME (MSK), exchange clock
    trade_date     date NOT NULL,             -- TRADEDATE
    session_date   date,                      -- TRADE_SESSION_DATE (FORTS clearing session)
    price          numeric NOT NULL,
    quantity       numeric NOT NULL,          -- contracts
    buysell        text CHECK (buysell IN ('B', 'S')),  -- aggressor side
    open_position  numeric,                   -- OPENPOSITION after the trade
    off_market     boolean,                   -- OFFMARKETDEAL
    rec_no         bigint,                    -- RECNO
    source_ts      timestamptz NOT NULL,      -- SYSTIME (exchange system time of the trade)
    latency        text NOT NULL CHECK (latency IN ('realtime', 'delayed')),  -- by feed: APIM realtime, ISS delayed
    run_id         bigint,
    capture_id     bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (secid, source, trade_no)
);
CREATE INDEX futures_trade_ts_idx ON md.futures_trade (trade_ts);
CREATE INDEX futures_trade_secid_ts_idx ON md.futures_trade (secid, trade_ts);
CREATE INDEX futures_trade_capture_idx ON md.futures_trade (capture_id);
COMMENT ON TABLE md.futures_trade IS
    'FORTS trades of the rates contracts (/engines/futures/markets/forts/securities/{secid}/trades, incremental by TRADENO): '
    'APIM realtime or public ISS (15-min delayed); sources are separate series (a trade may exist under both)';

-- ------------------------------------------------------------ md.zcyc_security_quote
CREATE TABLE md.zcyc_security_quote (
    secid              text NOT NULL,
    ts                 timestamptz NOT NULL,  -- tradedate + updatetime (MSK): quote time
    trade_date         date NOT NULL,
    calc_ts            timestamptz,           -- tradedate + tradetime (MSK): KBD calculation time
    short_name         text,
    benchmark          boolean,
    exp_date           date,                  -- maturity
    bid_price_pct      numeric,               -- % of face
    bid_yield_pct      numeric,               -- % p.a. (MOEX effective yield)
    ask_price_pct      numeric,
    ask_yield_pct      numeric,
    trd_price_pct      numeric,               -- last trade
    trd_yield_pct      numeric,
    clc_price_pct      numeric,               -- price from the KBD
    clc_yield_pct      numeric,
    crt_price_pct      numeric,               -- corrected (fair) price
    crt_yield_pct      numeric,
    correction         numeric,               -- as published
    crt_duration_days  numeric,
    bid_duration_days  numeric,
    ask_duration_days  numeric,
    source_ts          timestamptz NOT NULL,  -- = calc_ts (the block has no SYSTIME)
    latency            text NOT NULL CHECK (latency IN ('realtime', 'delayed')),
    run_id             bigint,
    capture_id         bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz,
    PRIMARY KEY (secid, ts)
);
CREATE INDEX zcyc_security_quote_ts_idx ON md.zcyc_security_quote (ts);
CREATE INDEX zcyc_security_quote_capture_idx ON md.zcyc_security_quote (capture_id);
COMMENT ON TABLE md.zcyc_security_quote IS
    'ISS /engines/stock/zcyc.json block securities: per OFZ the order-book bid/ask, last trade and KBD prices/yields '
    '(~29 OFZ-PD, near realtime on public ISS); a row is stored when a value changed for the secid';
CALL ops.enable_history('md.zcyc_security_quote', ARRAY['secid', 'ts']);

-- ------------------------------------------------------------------ md.index_intraday
CREATE TABLE md.index_intraday (
    secid            text NOT NULL,
    ts               timestamptz NOT NULL,    -- UPDATETIME on the SYSTIME session day (MSK)
    board            text,
    trade_date       date,                    -- TRADEDATE
    current_value    numeric,                 -- CURRENTVALUE
    last_value       numeric,                 -- LASTVALUE (previous close)
    open_value       numeric,
    high             numeric,
    low              numeric,
    last_change      numeric,
    last_change_prc  numeric,                 -- % vs previous close
    val_today        numeric,                 -- RUB
    trading_session  text,
    source_ts        timestamptz NOT NULL,    -- SYSTIME
    latency          text NOT NULL CHECK (latency IN ('realtime', 'delayed')),
    run_id           bigint,
    capture_id       bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz,
    PRIMARY KEY (secid, ts)
);
CREATE INDEX index_intraday_ts_idx ON md.index_intraday (ts);
CREATE INDEX index_intraday_capture_idx ON md.index_intraday (capture_id);
COMMENT ON TABLE md.index_intraday IS
    'ISS /engines/stock/markets/index/securities marketdata (RGBI, RGBITR, ...: realtime on public ISS); changed values only';
CALL ops.enable_history('md.index_intraday', ARRAY['secid', 'ts']);

-- -------------------------------------------------------------------- md.index_candle
CREATE TABLE md.index_candle (
    secid         text NOT NULL,
    interval_min  integer NOT NULL CHECK (interval_min > 0),
    begin_ts      timestamptz NOT NULL,       -- candle begin (MSK as published)
    end_ts        timestamptz NOT NULL,
    trade_date    date NOT NULL,
    open          numeric,
    high          numeric,
    low           numeric,
    close         numeric,
    value         numeric,
    volume        numeric,
    source_ts     timestamptz NOT NULL,       -- end_ts (last tick the candle can contain)
    latency       text NOT NULL CHECK (latency IN ('realtime', 'delayed')),
    run_id        bigint,
    capture_id    bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz,
    PRIMARY KEY (secid, interval_min, begin_ts)
);
CREATE INDEX index_candle_trade_date_idx ON md.index_candle (trade_date);
CREATE INDEX index_candle_begin_idx ON md.index_candle (begin_ts);
CREATE INDEX index_candle_capture_idx ON md.index_candle (capture_id);
COMMENT ON TABLE md.index_candle IS
    'ISS /engines/stock/markets/index/securities/{secid}/candles (interval=1); the candle of the running minute is revised (row history)';
CALL ops.enable_history('md.index_candle', ARRAY['secid', 'interval_min', 'begin_ts']);

-- --------------------------------------------------------------- md.index_constituent
CREATE TABLE md.index_constituent (
    index_id         text NOT NULL,
    trade_date       date NOT NULL,
    secid            text NOT NULL,
    ticker           text,
    short_name       text,
    weight_pct       numeric NOT NULL,        -- % of the index (sum ~ 100)
    trading_session  integer,
    session_date     date,
    run_id           bigint,
    capture_id       bigint REFERENCES ops.raw_capture (id),
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz,
    PRIMARY KEY (index_id, trade_date, secid)
);
CREATE INDEX index_constituent_secid_idx ON md.index_constituent (secid, trade_date);
CREATE INDEX index_constituent_capture_idx ON md.index_constituent (capture_id);
COMMENT ON TABLE md.index_constituent IS
    'ISS /statistics/engines/stock/markets/index/analytics/{index}?date=: composition and weights (validated: rows = cursor TOTAL, weights sum ~ 100)';
CALL ops.enable_history('md.index_constituent', ARRAY['index_id', 'trade_date', 'secid']);

-- ------------------------------------------------------------------- md.repo_intraday
CREATE TABLE md.repo_intraday (
    market          text NOT NULL CHECK (market IN ('ccp', 'gcc')),
    board           text NOT NULL,            -- EQRP/EQWP/EQMP/EQTP (ccp), GCRP/GCOW/... (gcc)
    secid           text NOT NULL,            -- bond SECID (ccp) or basket code (gcc)
    ts              timestamptz NOT NULL,     -- UPDATETIME on the SYSTIME session day (MSK)
    trade_date      date NOT NULL,
    settle_date     date,                     -- first leg (SETTLEDATE)
    settle_date2    date,                     -- second leg (SETTLEDATE2, ccp only)
    bid_pct         numeric,                  -- repo rates, % p.a.
    offer_pct       numeric,
    open_pct        numeric,
    low_pct         numeric,
    high_pct        numeric,
    last_pct        numeric,
    waprice_pct     numeric,
    num_trades      integer,
    vol_today       numeric,
    val_today_rub   numeric,
    trading_status  text,
    source_ts       timestamptz NOT NULL,     -- SYSTIME
    latency         text NOT NULL CHECK (latency IN ('realtime', 'delayed')),
    run_id          bigint,
    capture_id      bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz,
    PRIMARY KEY (market, board, secid, ts)
);
CREATE INDEX repo_intraday_ts_idx ON md.repo_intraday (ts);
CREATE INDEX repo_intraday_secid_ts_idx ON md.repo_intraday (secid, ts);
CREATE INDEX repo_intraday_capture_idx ON md.repo_intraday (capture_id);
COMMENT ON TABLE md.repo_intraday IS
    'MOEX repo with the CCP marketdata snapshots: market ccp (per OFZ, boards EQRP 1d, EQWP 7d, EQMP 1m, EQTP 3m) '
    'and gcc (GC baskets by term); changed rows only per (market, board, secid)';
CALL ops.enable_history('md.repo_intraday', ARRAY['market', 'board', 'secid', 'ts']);

-- ------------------------------------------------------------------------ md.repo_eod
CREATE TABLE md.repo_eod (
    market           text NOT NULL CHECK (market IN ('ccp', 'gcc')),
    trade_date       date NOT NULL,
    board            text NOT NULL,
    secid            text NOT NULL,
    short_name       text,
    num_trades       integer,
    value_rub        numeric,
    open_pct         numeric,
    low_pct          numeric,
    high_pct         numeric,
    close_pct        numeric,
    waprice_pct      numeric,
    trading_session  integer,
    run_id           bigint,
    capture_id       bigint REFERENCES ops.raw_capture (id),
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz,
    PRIMARY KEY (market, trade_date, board, secid)
);
CREATE INDEX repo_eod_secid_idx ON md.repo_eod (secid, trade_date);
CREATE INDEX repo_eod_capture_idx ON md.repo_eod (capture_id);
COMMENT ON TABLE md.repo_eod IS
    'ISS /history/engines/stock/markets/{ccp|gcc}/boards/{board}/securities?date=: daily repo rates (% p.a.); ccp rows only for OFZ';
CALL ops.enable_history('md.repo_eod', ARRAY['market', 'trade_date', 'board', 'secid']);

-- ---------------------------------------------------------------- md.bond_close_quote
CREATE TABLE md.bond_close_quote (
    trade_date      date NOT NULL,
    secid           text NOT NULL,
    board           text NOT NULL,            -- trading board; 'ZCYC' for basis zcyc_securities
    basis           text NOT NULL CHECK (basis IN ('bonds_marketdata', 'zcyc_securities')),
    cut_ts          timestamptz NOT NULL,     -- the close cut (MDHUB_BOND_CLOSE_CUT on trade_date)
    quote_ts        timestamptz NOT NULL,     -- time of the quote used (latest at or before cut_ts)
    close_bid_pct   numeric,                  -- % of face
    close_ask_pct   numeric,
    bid_yield_pct   numeric,                  -- zcyc_securities only
    ask_yield_pct   numeric,
    source_ts       timestamptz NOT NULL,
    latency         text NOT NULL CHECK (latency IN ('realtime', 'delayed')),
    run_id          bigint,
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz,
    PRIMARY KEY (trade_date, secid, board, basis)
);
CREATE INDEX bond_close_quote_secid_idx ON md.bond_close_quote (secid, trade_date);
COMMENT ON TABLE md.bond_close_quote IS
    'Closing bid/ask per bond: the latest stored quote at or before the close cut of md.bond_quote_intraday '
    '(basis bonds_marketdata, per board) and md.zcyc_security_quote (basis zcyc_securities, board ZCYC); derived, no forward fill across days';
CALL ops.enable_history('md.bond_close_quote', ARRAY['trade_date', 'secid', 'board', 'basis']);
