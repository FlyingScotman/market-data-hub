-- 0100_ref_bonds: bond static / reference data (DESIGN §4 "ref"), loaded by
-- mdhub.loaders.bonds_static (MOEX ISS) and mdhub.loaders.cbonds_enrich (Cbonds, optional).
--
-- Conventions
-- * Every table is upserted by its natural key with mdhub.core.upsert.upsert_rows; identical
--   reloads are no-ops. Row history (ops.row_history) is enabled on every table, so term
--   revisions (changed coupon/record dates, offer status, face value...) are kept.
-- * Rows are never deleted by loaders: schedule rows that disappear from a complete MOEX
--   snapshot get is_deleted = true (a revision), boards that are no longer listed get
--   listed = false; last_seen / last_seen_run tell when a row was last observed.
-- * Money and rates are numeric; *_pct = percent (14.5 = 14.5 %), *_bp = basis points.
-- * Currencies are ISO codes (MOEX 'SUR' is normalized to 'RUB').

-- ----------------------------------------------------------------------------- ref.issuer
CREATE TABLE ref.issuer (
    issuer_id     bigint PRIMARY KEY,           -- MOEX emitent_id
    name          text,
    short_name    text,
    inn           text,
    ogrn          text,
    okpo          text,
    country       text,
    sector        text,
    is_sovereign  boolean NOT NULL DEFAULT false,
    source        text NOT NULL DEFAULT 'moex_iss',
    run_id        bigint,
    capture_id    bigint REFERENCES ops.raw_capture (id),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_run bigint
);
CREATE INDEX issuer_inn_idx ON ref.issuer (inn);
COMMENT ON TABLE ref.issuer IS 'Bond issuers keyed by MOEX emitent_id (ISS /securities.json directory)';
COMMENT ON COLUMN ref.issuer.is_sovereign IS 'true for the Ministry of Finance of the Russian Federation (INN 7710168360)';

-- ------------------------------------------------------------------------------- ref.bond
CREATE TABLE ref.bond (
    secid                     text PRIMARY KEY,
    isin                      text,
    regnumber                 text,
    issuer_id                 bigint,
    name                      text,
    short_name                text,
    lat_name                  text,
    sec_type                  text,          -- MOEX TYPE: ofz_bond, exchange_bond, corporate_bond, ...
    type_name                 text,          -- MOEX TYPENAME
    sec_subtype               text,          -- MOEX SECSUBTYPE
    bond_type                 text,          -- MOEX label BOND_TYPE / BONDTYPE (verbatim)
    bond_subtype              text,          -- MOEX label BOND_SUBTYPE / BONDSUBTYPE (verbatim)
    coupon_details            text,          -- MOEX label COUPON_DETAILS (verbatim)
    facevalue_type            text,          -- MOEX label FACEVALUE_TYPE (verbatim)
    bond_class                text NOT NULL,
    coupon_type               text NOT NULL,
    coupon_type_basis         text NOT NULL,
    coupon_benchmark          text,          -- MOEX COUPON_BENCHMARK code (RREFKEYR, RUONIA, ...)
    coupon_benchmark_spread_pct numeric,     -- MOEX COUPON_BENCHMARK_SPREAD (percentage points)
    face_value                numeric,       -- current face (after amortization / indexation)
    initial_face_value        numeric,
    face_unit                 text,          -- face (nominal) currency
    currency_id               text,          -- settlement currency on the primary board
    issue_date                date,
    maturity_date             date,          -- NULL for perpetuals
    is_perpetual              boolean NOT NULL DEFAULT false,
    start_date_moex           date,          -- STARTDATEMOEX: first trading day on MOEX
    registry_date             date,
    coupon_frequency          integer,       -- coupons per year (COUPONFREQUENCY)
    coupon_period_days        integer,       -- current period length (COUPONPERIOD, primary board)
    day_count                 text,          -- written by cbonds_enrich only (MOEX has no field)
    day_count_source          text,
    listing_level             integer,
    issue_size                numeric,       -- number of bonds (ISSUESIZE)
    issue_size_placed         numeric,
    issue_size_planned        numeric,
    program_regnumber         text,
    is_amortizing             boolean,
    is_subordinated           boolean,
    is_structured             boolean,
    is_convertible            boolean,
    is_face_indexed           boolean,
    is_fx_face                boolean,       -- face_unit <> 'RUB'
    qualified_only            boolean,
    qual_investor_group       text,
    high_risk                 boolean,
    primary_board             text,
    marketprice_board         text,
    is_traded                 boolean,       -- MOEX directory is_traded
    in_directory              boolean NOT NULL DEFAULT true,
    description_source        text,          -- collection | security | NULL (no description)
    status                    text NOT NULL,
    source                    text NOT NULL DEFAULT 'moex_iss',
    first_seen_at             timestamptz NOT NULL DEFAULT now(),
    run_id                    bigint,
    capture_id                bigint REFERENCES ops.raw_capture (id),
    ingested_at               timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    last_seen_run             bigint,
    CONSTRAINT bond_status_chk CHECK (status IN ('active', 'matured', 'delisted')),
    CONSTRAINT bond_class_chk CHECK (bond_class IN (
        'ofz_pd', 'ofz_pk', 'ofz_in', 'ofz_ad', 'ofz_n', 'sovereign_fx', 'sovereign_other',
        'foreign_sovereign',
        'cbr_bond', 'ifi', 'subfederal', 'municipal', 'structured', 'mbs', 'securitization',
        'corporate', 'unknown')),
    CONSTRAINT bond_coupon_type_chk CHECK (coupon_type IN (
        'fixed', 'floating', 'indexed_face', 'zero', 'variable_known', 'unknown')),
    CONSTRAINT bond_coupon_type_basis_chk CHECK (coupon_type_basis IN (
        'moex_label', 'benchmark', 'ofz_series', 'schedule', 'none')),
    CONSTRAINT bond_description_source_chk CHECK (
        description_source IS NULL OR description_source IN ('collection', 'security'))
);
CREATE INDEX bond_isin_idx ON ref.bond (isin);
CREATE INDEX bond_issuer_idx ON ref.bond (issuer_id);
CREATE INDEX bond_class_status_idx ON ref.bond (bond_class, status);
CREATE INDEX bond_status_idx ON ref.bond (status);
COMMENT ON TABLE ref.bond IS 'All MOEX bonds (stock_bonds group: traded, archived, structured notes); one row per SECID';
COMMENT ON COLUMN ref.bond.bond_class IS
    'Derived: ofz_pd/pk/in/ad/n (Minfin RUB series), sovereign_fx (Minfin FX face), sovereign_other, '
    'foreign_sovereign (MOEX ofz_bond of another state), '
    'cbr_bond, ifi, subfederal, municipal, structured, mbs (mortgage agents), securitization (SFO), '
    'corporate (incl. MOEX exchange_bond/non_exchange_bond; see sec_type)';
COMMENT ON COLUMN ref.bond.coupon_type IS
    'Derived from current MOEX labels (COUPON_DETAILS, BOND_TYPE, FACEVALUE_TYPE, COUPON_BENCHMARK); '
    'floating = benchmark-linked coupon, indexed_face = linker with fixed coupon on indexed face, '
    'variable_known = fixed coupons announced period by period';
COMMENT ON COLUMN ref.bond.coupon_type_basis IS
    'moex_label | benchmark | ofz_series | schedule (matured or unlabelled bonds: from the published schedule) | none';
COMMENT ON COLUMN ref.bond.status IS
    'matured: maturity_date < today (MSK); active: on a MOEX board (directory is_traded or market list); else delisted';

-- ------------------------------------------------------------------------- ref.bond_board
CREATE TABLE ref.bond_board (
    secid                     text NOT NULL,
    board                     text NOT NULL,
    engine                    text,
    market                    text,
    title                     text,
    is_primary                boolean NOT NULL DEFAULT false,
    board_is_traded           boolean,        -- per-SECID boards block is_traded
    market_status             text,           -- bonds market list STATUS (A / N)
    lot_size                  integer,
    min_step                  numeric,
    decimals                  integer,
    currency_id               text,           -- settlement currency on this board
    face_unit                 text,
    settle_lag_days           integer,        -- business days trade_date -> SETTLEDATE (calendar of the run)
    settle_code               text,           -- 'T0', 'T1', ... derived from settle_lag_days
    listed_from               date,
    listed_till               date,
    listed                    boolean NOT NULL DEFAULT true,
    listing_source            text NOT NULL DEFAULT 'market'
                              CHECK (listing_source IN ('market', 'description')),
    settle_date               date,           -- observation (meta): SETTLEDATE for settle_trade_date
    settle_trade_date         date,           -- observation (meta)
    face_value_on_settle_date numeric,        -- observation (meta): FACEVALUEONSETTLEDATE
    source                    text NOT NULL DEFAULT 'moex_iss',
    first_seen                date NOT NULL DEFAULT (now() AT TIME ZONE 'Europe/Moscow')::date,
    last_seen                 date,
    run_id                    bigint,
    capture_id                bigint REFERENCES ops.raw_capture (id),
    ingested_at               timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    last_seen_run             bigint,
    PRIMARY KEY (secid, board),
    CONSTRAINT bond_board_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX bond_board_board_idx ON ref.bond_board (board) WHERE listed;
COMMENT ON TABLE ref.bond_board IS
    'Board listings: every row of the ISS bonds market list (TQOB, TQCB, TQOD, TQRD, TQOY, SPOB, ...) '
    'plus all boards of the per-SECID description (EQRP structured notes, repo/OTC boards). '
    'listing_source: market (bonds market list) or description (per-SECID boards block). '
    'listed=false: not in the latest observation of its listing_source (never deleted)';

-- ------------------------------------------------------------------------ ref.bond_coupon
CREATE TABLE ref.bond_coupon (
    secid         text NOT NULL,
    coupon_no     integer NOT NULL CHECK (coupon_no > 0),
    start_date    date,
    pay_date      date NOT NULL,
    record_date   date,
    amount        numeric,        -- per bond, in face_unit (MOEX value)
    value_rub     numeric,        -- MOEX value_rub; NULL for future coupons of FX-face bonds (MOEX converts at today's rate)
    value_prc     numeric,        -- coupon rate % p.a. (MOEX valueprc)
    face_value    numeric,
    face_unit     text,
    status        text NOT NULL CHECK (status IN ('known', 'projected_by_moex', 'unknown')),
    is_deleted    boolean NOT NULL DEFAULT false,
    source        text NOT NULL DEFAULT 'moex_iss',
    run_id        bigint,
    capture_id    bigint REFERENCES ops.raw_capture (id),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_run bigint,
    PRIMARY KEY (secid, coupon_no),
    CONSTRAINT bond_coupon_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX bond_coupon_pay_date_idx ON ref.bond_coupon (pay_date);
COMMENT ON TABLE ref.bond_coupon IS
    'Coupon schedule from MOEX bondization. coupon_no = 1-based position of the coupon in the '
    'bond''s complete MOEX schedule ordered by (pay_date, start_date, record_date); a moved record or '
    'pay date is a revision of the same coupon_no (ops.row_history), not a new row';
COMMENT ON COLUMN ref.bond_coupon.status IS
    'known: published amount/rate; projected_by_moex: MOEX value that is not a fixed amount yet '
    '(floating coupons beyond the current period, in-arrears benchmarks, amounts on an indexed face); '
    'unknown: no value published (amount/rate NULL)';

-- ------------------------------------------------------------------ ref.bond_amortization
CREATE TABLE ref.bond_amortization (
    secid              text NOT NULL,
    amort_date         date NOT NULL,
    data_source        text NOT NULL,   -- MOEX: amortization | maturity
    value_prc          numeric,         -- % of initial face
    value              numeric,         -- per bond, in face_unit
    value_rub          numeric,         -- NULL for future rows of FX-face bonds (converted at today's rate)
    face_value         numeric,
    initial_face_value numeric,
    face_unit          text,
    is_deleted         boolean NOT NULL DEFAULT false,
    source             text NOT NULL DEFAULT 'moex_iss',
    run_id             bigint,
    capture_id         bigint REFERENCES ops.raw_capture (id),
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    last_seen_run      bigint,
    PRIMARY KEY (secid, amort_date, data_source),
    CONSTRAINT bond_amortization_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE ref.bond_amortization IS 'Principal schedule (amortizations + final redemption) from MOEX bondization';

-- ------------------------------------------------------------------------- ref.bond_offer
CREATE TABLE ref.bond_offer (
    secid             text NOT NULL,
    source            text NOT NULL DEFAULT 'moex_iss',
    offer_key         text NOT NULL,   -- offer_date ISO ('#n' suffix for same-date duplicates) or 'undated-n'
    offer_date        date,            -- NULL when the source date is missing / '0000-00-00'
    date_missing      boolean NOT NULL DEFAULT false,
    offer_type        text NOT NULL CHECK (offer_type IN ('put', 'call', 'buyback', 'unknown')),
    offer_type_basis  text,            -- put_option_date | call_option_date | buyback_date | exercise_window | bond_subtype | source_type
    offer_type_raw    text,            -- MOEX offertype / Cbonds option type, verbatim
    status            text NOT NULL CHECK (status IN (
                          'scheduled', 'completed', 'cancelled', 'default', 'technical_default',
                          'past', 'undated')),
    is_offer_or_redemption boolean NOT NULL DEFAULT false,  -- MOEX 'Оферта/Погашение'
    offer_date_start  date,            -- holder submission window
    offer_date_end    date,
    price             numeric,         -- % of face
    value             numeric,         -- per bond, in face_unit
    face_value        numeric,
    face_unit         text,
    agent             text,
    is_deleted        boolean NOT NULL DEFAULT false,
    run_id            bigint,
    capture_id        bigint REFERENCES ops.raw_capture (id),
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_run     bigint,
    PRIMARY KEY (secid, source, offer_key),
    CONSTRAINT bond_offer_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX bond_offer_date_idx ON ref.bond_offer (offer_date);
COMMENT ON TABLE ref.bond_offer IS
    'Offers / options. MOEX rows (source moex_iss) from bondization; Cbonds rows (source cbonds) '
    'from get_offert. date_missing marks MOEX ''0000-00-00'' rows (offer_date NULL)';

-- ------------------------------------------------------------------- ref.bond_rate_formula
CREATE TABLE ref.bond_rate_formula (
    secid          text NOT NULL,
    source         text NOT NULL,          -- moex_iss | cbonds | curated
    benchmark      text,                   -- KEY_RATE, RUONIA, RUONIA_3M, RUONIA_6M, RUONIA_INDEX, CPI, ZCYC, OTHER
    benchmark_raw  text,                   -- source code / name verbatim
    spread_bp      numeric,
    multiplier     numeric,
    lag_days       integer,
    averaging      text,
    floor_pct      numeric,
    cap_pct        numeric,
    variable_rate_type text,               -- Cbonds variable_rate_type_id (verbatim)
    fixing_day     integer,                -- Cbonds floating_rates_day
    status         text NOT NULL CHECK (status IN ('curated', 'inferred', 'partial')),
    evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_deleted     boolean NOT NULL DEFAULT false,
    run_id         bigint,
    capture_id     bigint REFERENCES ops.raw_capture (id),
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_run  bigint,
    PRIMARY KEY (secid, source),
    CONSTRAINT bond_rate_formula_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE ref.bond_rate_formula IS
    'Floater formula per source. MOEX rows: benchmark + spread from COUPON_BENCHMARK(_SPREAD) / '
    'COUPON_DETAILS, status inferred (partial without spread); lag/averaging/floor/cap are NULL '
    'unless a source states them (never guessed). Resolved view: ref.bond_rate_formula_resolved';

-- ------------------------------------------------------------------------ ref.bond_rating
CREATE TABLE ref.bond_rating (
    secid         text NOT NULL,
    agency        text NOT NULL,
    scale         text NOT NULL DEFAULT '',
    source        text NOT NULL,
    issuer_id     bigint,
    rating        text,
    outlook       text,
    rating_date   date,
    run_id        bigint,
    capture_id    bigint REFERENCES ops.raw_capture (id),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_run bigint,
    PRIMARY KEY (secid, agency, scale, source),
    CONSTRAINT bond_rating_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE ref.bond_rating IS 'Latest issue rating per agency/scale (Cbonds get_rating_emission_maxdate); changes in ops.row_history';

-- ---------------------------------------------------------------------- ref.static_snapshot
CREATE TABLE ref.static_snapshot (
    secid          text NOT NULL,
    source         text NOT NULL,
    endpoint       text NOT NULL,          -- directory | collection | market | description | bondization | cbonds_*
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    capture_id     bigint REFERENCES ops.raw_capture (id),
    run_id         bigint,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    last_seen      timestamptz,
    last_seen_run  bigint,
    PRIMARY KEY (secid, source, endpoint, payload_sha256)
);
CREATE INDEX static_snapshot_latest_idx ON ref.static_snapshot (endpoint, secid, last_seen DESC);
COMMENT ON TABLE ref.static_snapshot IS
    'Per-SECID observation log: one row per distinct content (SHA-256 of the canonical JSON of the '
    'source rows for that SECID) and endpoint; capture_id = first capture holding it, last_seen = '
    'last observation. Description rows point to the full per-SECID payload (re-parsed between refreshes)';

-- ----------------------------------------------------------------------- ref.static_conflict
CREATE TABLE ref.static_conflict (
    secid            text NOT NULL,
    field            text NOT NULL,
    source_a         text NOT NULL,       -- selected source
    source_b         text NOT NULL,       -- conflicting source
    value_a          text,
    value_b          text,
    selected_source  text NOT NULL,
    reason           text,
    resolved         boolean NOT NULL DEFAULT false,
    first_detected_at timestamptz NOT NULL DEFAULT now(),
    run_id           bigint,
    capture_id       bigint REFERENCES ops.raw_capture (id),
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_run    bigint,
    PRIMARY KEY (secid, field, source_a, source_b)
);
COMMENT ON TABLE ref.static_conflict IS
    'Automatic cross-source disagreements (e.g. MOEX vs Cbonds maturity, coupon frequency, benchmark). '
    'Precedence: Cbonds > MOEX for conventions (day count, floater terms); MOEX > Cbonds for exchange '
    'schedule data. resolved=true once both sources agree again';

-- --------------------------------------------------------------- resolved formula view
CREATE VIEW ref.bond_rate_formula_resolved AS
SELECT
    b.secid,
    COALESCE(c.benchmark, m.benchmark, cb.benchmark)          AS benchmark,
    COALESCE(c.spread_bp, cb.spread_bp, m.spread_bp)           AS spread_bp,
    COALESCE(c.multiplier, cb.multiplier, m.multiplier)        AS multiplier,
    COALESCE(c.lag_days, cb.lag_days, m.lag_days)              AS lag_days,
    COALESCE(c.averaging, cb.averaging, m.averaging)           AS averaging,
    COALESCE(c.floor_pct, cb.floor_pct, m.floor_pct)           AS floor_pct,
    COALESCE(c.cap_pct, cb.cap_pct, m.cap_pct)                 AS cap_pct,
    COALESCE(c.variable_rate_type, cb.variable_rate_type)      AS variable_rate_type,
    COALESCE(c.fixing_day, cb.fixing_day)                      AS fixing_day,
    CASE WHEN c.secid IS NOT NULL THEN 'curated'
         WHEN cb.secid IS NOT NULL OR m.secid IS NOT NULL THEN
              CASE WHEN COALESCE(cb.status, m.status) = 'inferred' THEN 'inferred' ELSE 'partial' END
    END                                                        AS status,
    array_remove(ARRAY[
        CASE WHEN c.secid IS NOT NULL THEN 'curated' END,
        CASE WHEN cb.secid IS NOT NULL THEN 'cbonds' END,
        CASE WHEN m.secid IS NOT NULL THEN 'moex_iss' END], NULL) AS sources
FROM ref.bond b
LEFT JOIN ref.bond_rate_formula m  ON m.secid = b.secid AND m.source = 'moex_iss' AND NOT m.is_deleted
LEFT JOIN ref.bond_rate_formula cb ON cb.secid = b.secid AND cb.source = 'cbonds' AND NOT cb.is_deleted
LEFT JOIN ref.bond_rate_formula c  ON c.secid = b.secid AND c.source = 'curated' AND NOT c.is_deleted
WHERE m.secid IS NOT NULL OR cb.secid IS NOT NULL OR c.secid IS NOT NULL;
COMMENT ON VIEW ref.bond_rate_formula_resolved IS
    'Field-level precedence curated > cbonds > moex_iss for conventions; benchmark curated > moex_iss > cbonds';

-- ------------------------------------------------------------------------------ history
CALL ops.enable_history('ref.issuer', ARRAY['issuer_id']);
CALL ops.enable_history('ref.bond', ARRAY['secid']);
CALL ops.enable_history('ref.bond_board', ARRAY['secid', 'board'],
                        ARRAY['settle_date', 'settle_trade_date', 'face_value_on_settle_date']);
CALL ops.enable_history('ref.bond_coupon', ARRAY['secid', 'coupon_no']);
CALL ops.enable_history('ref.bond_amortization', ARRAY['secid', 'amort_date', 'data_source']);
CALL ops.enable_history('ref.bond_offer', ARRAY['secid', 'source', 'offer_key']);
CALL ops.enable_history('ref.bond_rate_formula', ARRAY['secid', 'source']);
CALL ops.enable_history('ref.bond_rating', ARRAY['secid', 'agency', 'scale', 'source']);
CALL ops.enable_history('ref.static_snapshot', ARRAY['secid', 'source', 'endpoint', 'payload_sha256']);
CALL ops.enable_history('ref.static_conflict', ARRAY['secid', 'field', 'source_a', 'source_b']);
