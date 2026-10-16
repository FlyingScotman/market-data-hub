-- 0210_md_fx_futures: MOEX FX (CETS / selt market) and FORTS FX futures.
-- Owner: loaders fx_moex.py / futures.py (DESIGN §4 ref.fx_instrument, ref.futures_contract,
-- md.fx_eod, md.fx_intraday, md.fx_indicative, md.futures_eod, md.futures_intraday + AlgoPack).
--
-- Conventions: prices are numeric exactly as MOEX publishes them ("raw"); columns suffixed
-- _rub_per_unit hold the futures price normalized to RUB per 1 unit of the base currency:
--     raw x STEPPRICE / MINSTEP / LOTVOLUME
-- (STEPPRICE in RUB per MINSTEP per lot). Perpetual futures (USDRUBF, EURRUBF, CNYRUBF) are
-- contract_type = 'perpetual' and are never spot.
-- capture_id of tables that may reference intraday captures ("intraday:*" endpoints, deleted
-- by raw retention) is ON DELETE SET NULL: ref.*, md.fx_intraday, md.futures_intraday, md.algopack_*.

-- ------------------------------------------------------------------ ref.fx_instrument
CREATE TABLE ref.fx_instrument (
    secid          text NOT NULL,
    board          text NOT NULL,
    short_name     text,
    sec_name       text,
    lat_name       text,
    base_ccy       text,              -- FACEUNIT as given (e.g. CNY, GLD, XAU)
    quote_ccy      text,              -- CURRENCYID as given (RUB, USD, ...)
    kind           text NOT NULL CHECK (kind IN ('outright', 'swap', 'fixing', 'wap', 'other')),
    tenor          text,              -- TOD/TOM/SPT/TDS/TMS/TDB/TMB/STS/FWD/FWS; swaps 'TOD/TOM', 'TOM/1W'
    near_leg       text,              -- swaps only
    far_leg        text,              -- swaps only
    lot_size       numeric,
    min_step       numeric,
    decimals       integer,
    face_value     numeric,
    market_code    text,
    status         text,
    settle_date    date,              -- as given by MOEX on last_seen (changes daily; not history)
    first_seen     date NOT NULL,
    last_seen      date,
    run_id         bigint,
    capture_id     bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz,
    PRIMARY KEY (secid, board)
);
COMMENT ON TABLE ref.fx_instrument IS
    'MOEX currency market (engine currency, market selt) instruments per board: CETS, CNGD, LICU, FIXN/FIXS (fixing auctions), WAPN/WAPS, SDBP';
COMMENT ON COLUMN ref.fx_instrument.settle_date IS
    'SETTLEDATE as published on the day of last_seen (never synthesized); excluded from row history';
CALL ops.enable_history('ref.fx_instrument', ARRAY['secid', 'board'], ARRAY['settle_date']);

-- --------------------------------------------------------------- ref.futures_contract
CREATE TABLE ref.futures_contract (
    secid            text PRIMARY KEY,
    board            text,
    short_name       text,
    sec_name         text,
    lat_name         text,
    asset_code       text,
    sec_type         text,
    is_fx            boolean NOT NULL DEFAULT false,
    base_ccy         text,               -- FX contracts only
    quote_ccy        text,               -- FX contracts only (RUB for Si/Eu/CR/perpetuals, USD for ED...)
    contract_type    text NOT NULL CHECK (contract_type IN ('dated', 'perpetual')),
    first_trade_date date,
    last_trade_date  date,               -- NULL for perpetuals (MOEX sentinel 2100-01-01)
    last_del_date    date,               -- NULL for perpetuals
    min_step         numeric,
    step_price       numeric,            -- RUB per min_step per lot; changes daily for crosses (ED, UCNY...)
    lot_volume       numeric,
    decimals         integer,
    spec_source      text NOT NULL CHECK (spec_source IN ('rfud', 'iss_description', 'history')),
    first_seen       date NOT NULL,
    last_seen        date,
    run_id           bigint,
    capture_id       bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz
);
CREATE INDEX futures_contract_asset_idx ON ref.futures_contract (asset_code, last_trade_date);
CREATE INDEX futures_contract_fx_idx ON ref.futures_contract (is_fx) WHERE is_fx;
COMMENT ON TABLE ref.futures_contract IS
    'FORTS futures (RFUD) static; is_fx marks currency futures (Si, Eu, CNY, ED, UCNY, ..., USDRUBF/EURRUBF/CNYRUBF)';
CALL ops.enable_history('ref.futures_contract', ARRAY['secid']);

-- ------------------------------------------------------------------------ md.fx_eod
CREATE TABLE md.fx_eod (
    trade_date   date NOT NULL,
    secid        text NOT NULL,
    board        text NOT NULL,
    open         numeric,
    low          numeric,
    high         numeric,
    close        numeric,
    waprice      numeric,
    num_trades   bigint,
    run_id       bigint,
    capture_id   bigint REFERENCES ops.raw_capture (id),
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, secid, board)
);
CREATE INDEX fx_eod_secid_idx ON md.fx_eod (secid, board, trade_date DESC);
COMMENT ON TABLE md.fx_eod IS
    'ISS /history/engines/currency/markets/selt (all boards). Prices are NULL when MOEX reports 0 with no trades. ISS publishes no volume/value for this market';
CALL ops.enable_history('md.fx_eod', ARRAY['trade_date', 'secid', 'board']);

-- -------------------------------------------------------------------- md.fx_intraday
CREATE TABLE md.fx_intraday (
    ts             timestamptz NOT NULL,     -- source UPDATETIME (MSK) on the SYSTIME date
    secid          text NOT NULL,
    board          text NOT NULL,
    source         text NOT NULL REFERENCES ops.source (code),  -- moex_iss, moex_passport, moex_apim, ratescalcs
    source_variant text NOT NULL CHECK (source_variant ~ '^[a-z][a-z0-9_]*$'),  -- iss_public, passport_cets_realtime, algopack_obstats_mid, ...
    bid            numeric,
    offer          numeric,
    last           numeric,
    waprice        numeric,
    open           numeric,
    high           numeric,
    low            numeric,
    num_trades     bigint,
    vol_today      numeric,
    val_today      numeric,
    trading_status text,
    source_ts      timestamptz,              -- SYSTIME
    latency        text NOT NULL CHECK (latency IN ('realtime', 'delayed', 'derived')),
    run_id         bigint,
    capture_id     bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (secid, board, source, source_variant, ts)
);
CREATE INDEX fx_intraday_ts_idx ON md.fx_intraday (ts);
COMMENT ON TABLE md.fx_intraday IS
    'FX spot intraday quotes; hub loader: CETS marketdata (source moex_iss, variant iss_public), a row only when values differ from the last stored row of (secid, board, source, source_variant). Legacy rows keep their own source/variant';

-- ------------------------------------------------------------------- md.fx_indicative
CREATE TABLE md.fx_indicative (
    trade_date   date NOT NULL,
    clearing     text NOT NULL CHECK (clearing IN ('tc', 'mc')),
    ccy_pair     text NOT NULL,              -- as given, e.g. 'USD/RUB'
    base_ccy     text NOT NULL,
    quote_ccy    text NOT NULL,
    trade_time   time,                       -- MSK, as given (14:00 tc / 19:00 mc; earlier years 13:45/18:49)
    rate         numeric NOT NULL,
    source       text NOT NULL DEFAULT 'moex',
    run_id       bigint,
    capture_id   bigint REFERENCES ops.raw_capture (id),
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, clearing, ccy_pair)
);
COMMENT ON TABLE md.fx_indicative IS
    'MOEX indicative FX rates (/statistics/engines/futures/markets/indicativerates): tc = intermediate clearing 14:00, mc = main clearing 19:00';
CALL ops.enable_history('md.fx_indicative', ARRAY['trade_date', 'clearing', 'ccy_pair']);

-- ----------------------------------------------------------------------- md.fx_fixing
CREATE TABLE md.fx_fixing (
    fixing_date  date NOT NULL,
    secid        text NOT NULL,              -- e.g. SRATE_CNY_ON
    rate         numeric,
    rate_pct     numeric,                    -- 'rateprc' where MOEX publishes it
    run_id       bigint,
    capture_id   bigint REFERENCES ops.raw_capture (id),
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fixing_date, secid)
);
COMMENT ON TABLE md.fx_fixing IS
    'MOEX currency-market statistics fixings (/statistics/engines/currency/markets/fixing), e.g. SRATE_CNY_ON';
CALL ops.enable_history('md.fx_fixing', ARRAY['fixing_date', 'secid']);

-- -------------------------------------------------------------------- md.futures_eod
CREATE TABLE md.futures_eod (
    trade_date           date NOT NULL,
    secid                text NOT NULL,
    board                text NOT NULL,
    asset_code           text,
    open                 numeric,
    low                  numeric,
    high                 numeric,
    close                numeric,
    settle_price         numeric,
    waprice              numeric,
    volume               numeric,            -- contracts
    value                numeric,            -- RUB
    open_position        numeric,            -- contracts
    open_position_value  numeric,            -- RUB
    num_trades           bigint,
    swap_rate            numeric,            -- perpetuals: SWAPRATE as published
    settle_rub_per_unit  numeric,
    close_rub_per_unit   numeric,
    run_id               bigint,
    capture_id           bigint REFERENCES ops.raw_capture (id),
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, secid)
);
CREATE INDEX futures_eod_secid_idx ON md.futures_eod (secid, trade_date DESC);
CREATE INDEX futures_eod_asset_idx ON md.futures_eod (asset_code, trade_date);
COMMENT ON TABLE md.futures_eod IS
    'ISS /history/engines/futures/markets/forts, all outright FORTS futures (calendar spreads skipped). *_rub_per_unit only for RUB-quoted FX contracts';
CALL ops.enable_history('md.futures_eod', ARRAY['trade_date', 'secid']);

-- --------------------------------------------------------------- md.futures_intraday
CREATE TABLE md.futures_intraday (
    ts                 timestamptz NOT NULL, -- source UPDATETIME (MSK) on the SYSTIME date
    secid              text NOT NULL,
    source             text NOT NULL REFERENCES ops.source (code),  -- moex_apim, moex_iss, ...
    source_variant     text NOT NULL CHECK (source_variant ~ '^[a-z][a-z0-9_]*$'),  -- apim_marketdata, iss_public, algopack_obstats_mid, ...
    trade_date         date,                 -- FORTS session date (TRADEDATE)
    bid                numeric,
    offer              numeric,
    last               numeric,
    open               numeric,
    high               numeric,
    low                numeric,
    settle_price       numeric,              -- current (intraday) settlement price estimate
    open_position      numeric,
    num_trades         bigint,
    vol_today          numeric,
    val_today          numeric,
    swap_rate          numeric,
    bid_rub_per_unit   numeric,
    offer_rub_per_unit numeric,
    last_rub_per_unit  numeric,
    source_ts          timestamptz,          -- SYSTIME
    latency            text NOT NULL CHECK (latency IN ('realtime', 'delayed', 'derived')),
    run_id             bigint,
    capture_id         bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (secid, source, source_variant, ts)
);
CREATE INDEX futures_intraday_ts_idx ON md.futures_intraday (ts);
COMMENT ON TABLE md.futures_intraday IS
    'FX futures intraday: APIM realtime (moex_apim/apim_marketdata), fallback public ISS (moex_iss/iss_public, delayed); change-only per (secid, source, source_variant)';

-- ------------------------------------------------------- md.algopack_fo_tradestats
CREATE TABLE md.algopack_fo_tradestats (
    interval_ts          timestamptz NOT NULL, -- tradedate + tradetime (MSK) as published
    secid                text NOT NULL,
    trade_date           date NOT NULL,
    asset_code           text,
    pr_open              numeric,
    pr_high              numeric,
    pr_low               numeric,
    pr_close             numeric,
    pr_std               numeric,
    vol                  numeric,
    val                  numeric,
    trades               bigint,
    pr_vwap              numeric,
    pr_change            numeric,
    trades_b             bigint,
    trades_s             bigint,
    val_b                numeric,
    val_s                numeric,
    vol_b                numeric,
    vol_s                numeric,
    disb                 numeric,
    pr_vwap_b            numeric,
    pr_vwap_s            numeric,
    im                   numeric,
    oi_open              numeric,
    oi_high              numeric,
    oi_low               numeric,
    oi_close             numeric,
    sec_pr_open          numeric,
    sec_pr_high          numeric,
    sec_pr_low           numeric,
    sec_pr_close         numeric,
    pr_close_rub_per_unit numeric,
    pr_vwap_rub_per_unit  numeric,
    source_ts            timestamptz,          -- SYSTIME
    run_id               bigint,
    capture_id           bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (secid, interval_ts)
);
CREATE INDEX algopack_fo_tradestats_date_idx ON md.algopack_fo_tradestats (trade_date);
COMMENT ON TABLE md.algopack_fo_tradestats IS
    'APIM AlgoPack /datashop/algopack/fo/tradestats, 5-minute bars of FX futures; one row per (secid, interval)';
CALL ops.enable_history('md.algopack_fo_tradestats', ARRAY['secid', 'interval_ts']);

-- --------------------------------------------------------- md.algopack_fo_obstats
CREATE TABLE md.algopack_fo_obstats (
    interval_ts           timestamptz NOT NULL,
    secid                 text NOT NULL,
    trade_date            date NOT NULL,
    asset_code            text,
    mid_price             numeric,
    micro_price           numeric,
    spread_l1             numeric,
    spread_l2             numeric,
    spread_l3             numeric,
    spread_l5             numeric,
    spread_l10            numeric,
    spread_l20            numeric,
    levels_b              integer,
    levels_s              integer,
    vol_b_l1              numeric,
    vol_b_l2              numeric,
    vol_b_l3              numeric,
    vol_b_l5              numeric,
    vol_b_l10             numeric,
    vol_b_l20             numeric,
    vol_s_l1              numeric,
    vol_s_l2              numeric,
    vol_s_l3              numeric,
    vol_s_l5              numeric,
    vol_s_l10             numeric,
    vol_s_l20             numeric,
    vwap_b_l3             numeric,
    vwap_b_l5             numeric,
    vwap_b_l10            numeric,
    vwap_b_l20            numeric,
    vwap_s_l3             numeric,
    vwap_s_l5             numeric,
    vwap_s_l10            numeric,
    vwap_s_l20            numeric,
    mid_price_rub_per_unit numeric,
    source_ts             timestamptz,
    run_id                bigint,
    capture_id            bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (secid, interval_ts)
);
CREATE INDEX algopack_fo_obstats_date_idx ON md.algopack_fo_obstats (trade_date);
COMMENT ON TABLE md.algopack_fo_obstats IS
    'APIM AlgoPack /datashop/algopack/fo/obstats (order-book stats), 5-minute, FX futures';
CALL ops.enable_history('md.algopack_fo_obstats', ARRAY['secid', 'interval_ts']);

-- ------------------------------------------------------------- md.algopack_futoi
CREATE TABLE md.algopack_futoi (
    interval_ts        timestamptz NOT NULL,
    ticker             text NOT NULL,        -- asset-level ticker as published (Si, Eu, CR, ED, UC, USDRUBF...)
    clgroup            text NOT NULL,        -- YUR (legal entities) / FIZ (individuals)
    trade_date         date NOT NULL,
    trade_session_date date,
    sess_id            bigint,
    seqnum             bigint,
    pos                numeric,
    pos_long           numeric,
    pos_short          numeric,
    pos_long_num       numeric,
    pos_short_num      numeric,
    source_ts          timestamptz,
    run_id             bigint,
    capture_id         bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, interval_ts, clgroup)
);
CREATE INDEX algopack_futoi_date_idx ON md.algopack_futoi (trade_date);
COMMENT ON TABLE md.algopack_futoi IS
    'APIM /analyticalproducts/futoi: open interest by client group, 5-minute, FX futures assets';
CALL ops.enable_history('md.algopack_futoi', ARRAY['ticker', 'interval_ts', 'clgroup']);
