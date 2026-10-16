-- 0250_md_official: official fixings (CBR key rate, RUONIA family, standing facility rates),
-- CBR official FX, CBR liquidity balance, ECB reference rates, dealer swap-curve snapshots
-- (Ratescalcs), CBR board meetings and business-day calendars.
-- DESIGN §4. ref.* tables live here (not in 0100-0199) because they are owned by the
-- official-data loaders (mdhub.loaders.cbr / mdhub.loaders.calendar).

-- ------------------------------------------------------------------------- md.fixing
CREATE TABLE md.fixing (
    index_code     text NOT NULL CHECK (index_code ~ '^[A-Z][A-Z0-9_]*$'),
    fixing_date    date NOT NULL,
    source         text NOT NULL REFERENCES ops.source (code),
    value          numeric NOT NULL,
    unit           text NOT NULL DEFAULT 'pct' CHECK (unit IN ('pct', 'index')),
    effective_date date,
    published_on   date,
    published_at   timestamptz,
    details        jsonb,
    capture_id     bigint REFERENCES ops.raw_capture (id),
    run_id         bigint,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (index_code, fixing_date, source),
    CONSTRAINT fixing_details_object CHECK (details IS NULL OR jsonb_typeof(details) = 'object')
);
CREATE INDEX fixing_date_idx ON md.fixing (fixing_date);
COMMENT ON TABLE md.fixing IS
    'Official fixings by observation/value date. KEY_RATE: CBR key rate per CBR business day '
    '(fixing_date = effective_date = the day the rate is in force; change history since 2013-09-17). '
    'RUONIA: observation date (published next business day, published_on). RUONIA_INDEX, '
    'TERM_RUONIA_1M/3M/6M: CBR compounded index and term (averaged) RUONIA per calendar day. '
    'CBR_DEPOSIT_STANDING / CBR_LENDING_STANDING: standing facility rates (O/N deposit, '
    'standing lending "main mechanism"). Corrections: previous images in ops.row_history.';
COMMENT ON COLUMN md.fixing.value IS 'Rate in % p.a. (unit=pct) or index level (unit=index)';
COMMENT ON COLUMN md.fixing.published_on IS 'Publication date stated by the source (RUONIA "Дата публикации")';
COMMENT ON COLUMN md.fixing.published_at IS 'Publication timestamp when the source states one (else NULL)';
COMMENT ON COLUMN md.fixing.details IS 'Source extras as strings (RUONIA volume, deals, percentiles, status...)';
COMMENT ON COLUMN md.fixing.ingested_at IS
    'Known-at: when the hub first saw this value (refreshed only when the value changes)';
CALL ops.enable_history('md.fixing', ARRAY['index_code', 'fixing_date', 'source']);

-- -------------------------------------------------------------------- md.fx_official
CREATE TABLE md.fx_official (
    effective_date date NOT NULL,
    ccy            text NOT NULL CHECK (ccy ~ '^[A-Z]{3}$'),
    nominal        integer NOT NULL CHECK (nominal > 0),
    value          numeric NOT NULL CHECK (value > 0),
    rate_per_unit  numeric NOT NULL CHECK (rate_per_unit > 0),
    vunit_rate     numeric,
    record_date    date NOT NULL,
    set_date       date NOT NULL,
    cbr_id         text NOT NULL,
    source         text NOT NULL DEFAULT 'cbr' REFERENCES ops.source (code),
    capture_id     bigint REFERENCES ops.raw_capture (id),
    run_id         bigint,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (effective_date, ccy),
    CONSTRAINT fx_official_dates CHECK (record_date <= effective_date AND set_date < record_date)
);
CREATE INDEX fx_official_ccy_idx ON md.fx_official (ccy, effective_date);
COMMENT ON TABLE md.fx_official IS
    'CBR official rates, one row per CALENDAR day the rate is in force (weekends/holidays included). '
    'CBR sets rates on business day T, in force from T+1 (record_date) through the next setting day. '
    'The tail of the latest record is extended to the first RU business day >= record_date only.';
COMMENT ON COLUMN md.fx_official.value IS 'RUB per nominal units as published (Value)';
COMMENT ON COLUMN md.fx_official.rate_per_unit IS 'RUB per 1 unit = value / nominal (exact)';
COMMENT ON COLUMN md.fx_official.vunit_rate IS 'CBR VunitRate as published (rounded per-unit rate), if present';
COMMENT ON COLUMN md.fx_official.record_date IS 'CBR record date (ValCurs/@Date): first day the rate is in force';
COMMENT ON COLUMN md.fx_official.set_date IS 'Day CBR set the rate = record_date - 1 (CBR rule: set on T for T+1)';
CALL ops.enable_history('md.fx_official', ARRAY['effective_date', 'ccy']);

-- ------------------------------------------------------------------- md.cbr_liquidity
CREATE TABLE md.cbr_liquidity (
    observed_on                   date PRIMARY KEY,
    deficit_bn                    numeric NOT NULL,
    deficit_ex_corr_accounts_bn   numeric NOT NULL,
    cbr_claims_total_bn           numeric NOT NULL,
    repo_fxswap_auction_bn        numeric NOT NULL,
    secured_loans_auction_bn      numeric NOT NULL,
    repo_fxswap_standing_bn       numeric NOT NULL,
    secured_loans_standing_bn     numeric NOT NULL,
    cbr_obligations_total_bn      numeric NOT NULL,
    deposits_auction_bn           numeric NOT NULL,
    deposits_standing_bn          numeric NOT NULL,
    cobr_bn                       numeric NOT NULL,
    nonstandard_net_claims_bn     numeric NOT NULL,
    corr_accounts_bn              numeric NOT NULL,
    required_reserves_averaged_bn numeric NOT NULL,
    methodology_version           text NOT NULL,
    revision_no                   integer NOT NULL DEFAULT 1 CHECK (revision_no >= 1),
    capture_id                    bigint REFERENCES ops.raw_capture (id),
    run_id                        bigint,
    ingested_at                   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE md.cbr_liquidity IS
    'CBR banking-sector liquidity balance (/hd_base/bliquidity/, RUB bn, columns 2..15 of the '
    'published table; deficit +, surplus -). Current view; every revision increments revision_no '
    'and the superseded image is in ops.row_history (capture -> revision -> current view).';
COMMENT ON COLUMN md.cbr_liquidity.ingested_at IS 'Known-at of the current revision';
CALL ops.enable_history('md.cbr_liquidity', ARRAY['observed_on']);

-- ------------------------------------------------------------------------ md.ecb_fx
CREATE TABLE md.ecb_fx (
    rate_date    date NOT NULL,
    ccy          text NOT NULL CHECK (ccy ~ '^[A-Z]{3}$'),
    rate_per_eur numeric NOT NULL CHECK (rate_per_eur > 0),
    capture_id   bigint REFERENCES ops.raw_capture (id),
    run_id       bigint,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, ccy)
);
CREATE INDEX ecb_fx_ccy_idx ON md.ecb_fx (ccy, rate_date);
COMMENT ON TABLE md.ecb_fx IS 'ECB euro foreign exchange reference rates (units of ccy per 1 EUR), TARGET days';
CALL ops.enable_history('md.ecb_fx', ARRAY['rate_date', 'ccy']);

-- ---------------------------------------------------------- md.curve_snapshot / quote
CREATE TABLE md.curve_snapshot (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         text NOT NULL REFERENCES ops.source (code),
    dataset        text NOT NULL CHECK (dataset ~ '^[a-z][a-z0-9_]*$'),
    quote_basis    text NOT NULL CHECK (quote_basis IN ('bid_ask_mid', 'mid_only')),
    fetched_at     timestamptz NOT NULL,
    snapshot_date  date NOT NULL,
    source_ts      timestamptz,
    market_closed  boolean,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    parser_version text NOT NULL,
    label          text,
    meta           jsonb,
    capture_id     bigint REFERENCES ops.raw_capture (id),
    run_id         bigint,
    legacy_id      text,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT curve_snapshot_meta_object CHECK (meta IS NULL OR jsonb_typeof(meta) = 'object')
);
CREATE UNIQUE INDEX curve_snapshot_natural_idx ON md.curve_snapshot (source, dataset, fetched_at);
CREATE INDEX curve_snapshot_dataset_idx ON md.curve_snapshot (dataset, fetched_at DESC);
CREATE UNIQUE INDEX curve_snapshot_legacy_idx ON md.curve_snapshot (source, legacy_id)
    WHERE legacy_id IS NOT NULL;
COMMENT ON TABLE md.curve_snapshot IS
    'Dealer/vendor curve snapshots. A new row is written only when the normalized content '
    '(content_sha256 over sorted tenor/bid/ask) differs from the previous snapshot of the same '
    '(source, dataset); as-of queries take the latest snapshot with fetched_at <= T.';
COMMENT ON COLUMN md.curve_snapshot.snapshot_date IS 'Moscow date of fetched_at';
COMMENT ON COLUMN md.curve_snapshot.source_ts IS 'Source clock (Ratescalcs snapshot timestamp); NULL for pages without one';

CREATE TABLE md.curve_quote (
    snapshot_id bigint NOT NULL REFERENCES md.curve_snapshot (id) ON DELETE CASCADE,
    tenor       text NOT NULL,
    position    integer NOT NULL CHECK (position >= 0),
    tenor_months integer,
    tenor_days  integer,
    pillar_date date,
    bid_pct     numeric,
    ask_pct     numeric,
    mid_pct     numeric,
    extra       jsonb,
    PRIMARY KEY (snapshot_id, tenor),
    CONSTRAINT curve_quote_bid_le_ask CHECK (bid_pct IS NULL OR ask_pct IS NULL OR bid_pct <= ask_pct),
    CONSTRAINT curve_quote_extra_object CHECK (extra IS NULL OR jsonb_typeof(extra) = 'object')
);
COMMENT ON TABLE md.curve_quote IS
    'Quotes of a curve snapshot, % p.a.; mid = (bid+ask)/2 recomputed by the hub. tenor_months for '
    'M/Y tenors, tenor_days for D/W/ON; futures-implied yields use the futures ticker as tenor';

-- ------------------------------------------------------------------- ref.cbr_meeting
CREATE TABLE ref.cbr_meeting (
    meeting_date      date PRIMARY KEY,
    effective_date    date NOT NULL,
    is_estimated      boolean NOT NULL,
    is_extraordinary  boolean NOT NULL DEFAULT false,
    decision          text CHECK (decision IN ('hike', 'cut', 'hold')),
    decision_rate_pct numeric,
    prev_rate_pct     numeric,
    title             text,
    source            text NOT NULL DEFAULT 'cbr' REFERENCES ops.source (code),
    capture_id        bigint REFERENCES ops.raw_capture (id),
    run_id            bigint,
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz,
    CONSTRAINT cbr_meeting_effective CHECK (effective_date >= meeting_date)
);
COMMENT ON TABLE ref.cbr_meeting IS
    'CBR board meetings on the key rate (/dkp/cal_mp/). effective_date = next RU business day '
    '(rule) until confirmed by md.fixing KEY_RATE; decision filled after the meeting.';
COMMENT ON COLUMN ref.cbr_meeting.is_estimated IS
    'true while effective_date/decision are not confirmed by the published key-rate series';
CALL ops.enable_history('ref.cbr_meeting', ARRAY['meeting_date']);

-- ------------------------------------------------------------------ ref.calendar_day
CREATE TABLE ref.calendar_day (
    calendar        text NOT NULL
                    CHECK (calendar IN ('MOEX_STOCK', 'MOEX_FX', 'MOEX_FORTS', 'RU_PRODUCTION')),
    day             date NOT NULL,
    source          text NOT NULL,
    is_business_day boolean NOT NULL,
    known_at        timestamptz NOT NULL,
    version         text,
    note            text,
    capture_id      bigint REFERENCES ops.raw_capture (id),
    run_id          bigint,
    PRIMARY KEY (calendar, day, source)
);
CREATE INDEX calendar_day_lookup_idx ON ref.calendar_day (calendar, day, known_at DESC);
COMMENT ON TABLE ref.calendar_day IS
    'Business days per calendar and source; consumers take the row with the latest known_at per '
    '(calendar, day) (mdhub.core.bizday.DbCalendar). Sources: observed_* (past days from MOEX '
    'trading / CBR RUONIA observations), moex_iss_calendar (official MOEX calendar, current '
    'year), curated / curated_unverified (versioned file mdhub/data/calendar_curated.json).';
CALL ops.enable_history('ref.calendar_day', ARRAY['calendar', 'day', 'source'],
                        ARRAY['known_at', 'version', 'note']);

CREATE OR REPLACE VIEW ref.calendar_effective AS
SELECT DISTINCT ON (calendar, day) calendar, day, is_business_day, source, known_at, version
FROM ref.calendar_day
ORDER BY calendar, day, known_at DESC;
COMMENT ON VIEW ref.calendar_effective IS 'Latest-known business-day flag per (calendar, day)';
