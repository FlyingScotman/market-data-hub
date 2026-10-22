-- Floaters on the MOEX KBD (benchmark ZCYC, MOEX ZR_YLD_CRV): the terms a consumer needs to
-- project the coupon (contract market_snapshot/1.4): the KBD tenor, the fixing date rule,
-- the rounding of the KBD value and the margin per period (a margin reset at a put offer is
-- data, not the current spread_bp).
--
--   coupon rate % = round(Y_KBD(T; d_fix), rate_rounding_decimals) + spread_bp / 100
--   Y_KBD(T; d) = MOEX effective annual zero yield at the FIXED tenor T on day d,
--                 (exp(G(T) / 1e4) - 1) * 100 from the parameters of md.zcyc_snapshot
--   d_fix = fixing_offset-th day of fixing_offset_unit before fixing_anchor
--
-- Writers: job bond_terms_curated (source 'curated', from issue documents with evidence) and
-- job zcyc_floater_fit (source 'zcyc_fit', status 'inferred'): the hub fits T and the fixing
-- offset to the published coupon rates over its own ZCYC history and keeps the result only
-- when it reproduces them (reproduction). The resolved formula takes these terms from the
-- curated row first, then from the fit; reproduction always comes from the hub's check.

ALTER TABLE ref.bond_rate_formula
    ADD COLUMN benchmark_tenor_years  numeric CHECK (benchmark_tenor_years > 0),
    ADD COLUMN fixing_anchor          text CHECK (fixing_anchor IN ('accrual_start')),
    ADD COLUMN fixing_offset          integer CHECK (fixing_offset >= 0),
    ADD COLUMN fixing_offset_unit     text CHECK (fixing_offset_unit IN (
                                          'zcyc_publication_days', 'business_days',
                                          'calendar_days')),
    ADD COLUMN rate_rounding_decimals integer CHECK (rate_rounding_decimals BETWEEN 0 AND 6),
    ADD COLUMN spread_schedule        jsonb CHECK (jsonb_typeof(spread_schedule) = 'array'),
    ADD COLUMN reproduction           jsonb CHECK (jsonb_typeof(reproduction) = 'object');
COMMENT ON COLUMN ref.bond_rate_formula.benchmark_tenor_years IS
    'ZCYC: fixed KBD tenor T in years whose zero yield sets the coupon';
COMMENT ON COLUMN ref.bond_rate_formula.fixing_anchor IS
    'Date the fixing offset counts back from: accrual_start = start of the coupon period';
COMMENT ON COLUMN ref.bond_rate_formula.fixing_offset IS
    'N: the fixing day is the N-th day (fixing_offset_unit) before the anchor; 0 = the anchor '
    'itself, or the last such day before it';
COMMENT ON COLUMN ref.bond_rate_formula.fixing_offset_unit IS
    'zcyc_publication_days = days with an end-of-day KBD publication (md.zcyc_snapshot is_eod; '
    'in the past = MOEX_STOCK business days), business_days = MOEX_STOCK business days, '
    'calendar_days';
COMMENT ON COLUMN ref.bond_rate_formula.rate_rounding_decimals IS
    'The KBD yield in percent is rounded half-up to this many decimals before the margin is added';
COMMENT ON COLUMN ref.bond_rate_formula.spread_schedule IS
    'Margin per period: [{effective_from (start date of the first coupon period it applies to), '
    'spread_bp, basis (document | coupon_history_fit), evidence_document_url}] by effective_from; '
    'spread_bp of the row stays the current margin';
COMMENT ON COLUMN ref.bond_rate_formula.reproduction IS
    'zcyc_fit rows: the hub''s check of the published coupon rates (value_prc) against the '
    'formula over its ZCYC history: method (fit | check), tested terms, periods_checked / '
    'periods_matched, tolerance, max error, unmatched coupons, accepted';

-- Field-level precedence as in 0420, plus the ZCYC terms: curated > zcyc_fit.
CREATE OR REPLACE VIEW ref.bond_rate_formula_resolved AS
SELECT
    b.secid,
    COALESCE(c.benchmark, m.benchmark, cb.benchmark, lb.benchmark)             AS benchmark,
    COALESCE(c.spread_bp, cb.spread_bp, m.spread_bp, lb.spread_bp)             AS spread_bp,
    COALESCE(c.multiplier, cb.multiplier, m.multiplier, lb.multiplier)         AS multiplier,
    COALESCE(c.lag_days, cb.lag_days, m.lag_days, lb.lag_days)                 AS lag_days,
    COALESCE(c.averaging, cb.averaging, m.averaging, lb.averaging)             AS averaging,
    COALESCE(c.floor_pct, cb.floor_pct, m.floor_pct, lb.floor_pct)             AS floor_pct,
    COALESCE(c.cap_pct, cb.cap_pct, m.cap_pct, lb.cap_pct)                     AS cap_pct,
    COALESCE(c.variable_rate_type, cb.variable_rate_type, lb.variable_rate_type)
                                                                               AS variable_rate_type,
    COALESCE(c.fixing_day, cb.fixing_day, lb.fixing_day)                       AS fixing_day,
    CASE WHEN c.secid IS NOT NULL THEN 'curated'
         WHEN cb.secid IS NOT NULL OR m.secid IS NOT NULL OR lb.secid IS NOT NULL THEN
              CASE WHEN COALESCE(cb.status, m.status, lb.status) = 'inferred' THEN 'inferred'
                   ELSE 'partial' END
    END                                                                        AS status,
    array_remove(ARRAY[
        CASE WHEN c.secid IS NOT NULL THEN 'curated' END,
        CASE WHEN cb.secid IS NOT NULL THEN 'cbonds' END,
        CASE WHEN m.secid IS NOT NULL THEN 'moex_iss' END,
        CASE WHEN lb.secid IS NOT NULL THEN 'legacy_bc' END,
        CASE WHEN c.benchmark_tenor_years IS NULL AND zf.benchmark_tenor_years IS NOT NULL
             THEN 'zcyc_fit' END], NULL)                                       AS sources,
    CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.benchmark_tenor_years
         ELSE zf.benchmark_tenor_years END                                     AS benchmark_tenor_years,
    CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.fixing_anchor
         ELSE zf.fixing_anchor END                                             AS fixing_anchor,
    CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.fixing_offset
         ELSE zf.fixing_offset END                                             AS fixing_offset,
    CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.fixing_offset_unit
         ELSE zf.fixing_offset_unit END                                        AS fixing_offset_unit,
    CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.rate_rounding_decimals
         ELSE zf.rate_rounding_decimals END                                    AS rate_rounding_decimals,
    CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN 'document'
         WHEN zf.benchmark_tenor_years IS NOT NULL THEN 'coupon_history_fit' END AS fixing_basis,
    COALESCE(c.spread_schedule, zf.spread_schedule)                            AS spread_schedule,
    zf.reproduction                                                            AS reproduction
FROM ref.bond b
LEFT JOIN ref.bond_rate_formula m  ON m.secid = b.secid AND m.source = 'moex_iss' AND NOT m.is_deleted
LEFT JOIN ref.bond_rate_formula cb ON cb.secid = b.secid AND cb.source = 'cbonds' AND NOT cb.is_deleted
LEFT JOIN ref.bond_rate_formula c  ON c.secid = b.secid AND c.source = 'curated' AND NOT c.is_deleted
LEFT JOIN ref.bond_rate_formula lb ON lb.secid = b.secid AND lb.source = 'legacy_bc' AND NOT lb.is_deleted
LEFT JOIN ref.bond_rate_formula zf ON zf.secid = b.secid AND zf.source = 'zcyc_fit' AND NOT zf.is_deleted
WHERE m.secid IS NOT NULL OR cb.secid IS NOT NULL OR c.secid IS NOT NULL OR lb.secid IS NOT NULL;
COMMENT ON VIEW ref.bond_rate_formula_resolved IS
    'Field-level precedence curated > cbonds > moex_iss > legacy_bc for conventions; benchmark '
    'curated > moex_iss > cbonds > legacy_bc (legacy_bc = MOEX labels as of the legacy snapshot); '
    'ZCYC terms (tenor, fixing rule, rounding) all from curated when it states a tenor, else '
    'from zcyc_fit (fixing_basis); spread_schedule curated > zcyc_fit; reproduction = the hub''s '
    'check (zcyc_fit). A zcyc_fit row alone does not make a formula';

DROP FUNCTION ref.bond_rate_formula_resolved_known_at(timestamptz);
CREATE FUNCTION ref.bond_rate_formula_resolved_known_at(p_known_at timestamptz)
RETURNS TABLE (
    secid text, benchmark text, benchmark_raw text, benchmark_source text, spread_bp numeric, multiplier numeric, lag_days integer,
    averaging text, floor_pct numeric, cap_pct numeric, variable_rate_type text,
    fixing_day integer, status text, sources text[],
    benchmark_tenor_years numeric, fixing_anchor text, fixing_offset integer,
    fixing_offset_unit text, rate_rounding_decimals integer, fixing_basis text,
    spread_schedule jsonb, reproduction jsonb
)
LANGUAGE sql STABLE AS $$
    WITH f AS MATERIALIZED (
        SELECT * FROM ref.bond_rate_formula_known_at(p_known_at) x WHERE NOT x.is_deleted
    ), b AS MATERIALIZED (
        SELECT x.secid FROM ref.bond_known_at(p_known_at) x
    )
    SELECT
        b.secid,
        COALESCE(c.benchmark, m.benchmark, cb.benchmark, lb.benchmark),
        CASE WHEN c.benchmark IS NOT NULL THEN c.benchmark_raw
             WHEN m.benchmark IS NOT NULL THEN m.benchmark_raw
             WHEN cb.benchmark IS NOT NULL THEN cb.benchmark_raw
             WHEN lb.benchmark IS NOT NULL THEN lb.benchmark_raw END,
        CASE WHEN c.benchmark IS NOT NULL THEN 'curated'
             WHEN m.benchmark IS NOT NULL THEN 'moex_iss'
             WHEN cb.benchmark IS NOT NULL THEN 'cbonds'
             WHEN lb.benchmark IS NOT NULL THEN 'legacy_bc' END,
        COALESCE(c.spread_bp, cb.spread_bp, m.spread_bp, lb.spread_bp),
        COALESCE(c.multiplier, cb.multiplier, m.multiplier, lb.multiplier),
        COALESCE(c.lag_days, cb.lag_days, m.lag_days, lb.lag_days),
        COALESCE(c.averaging, cb.averaging, m.averaging, lb.averaging),
        COALESCE(c.floor_pct, cb.floor_pct, m.floor_pct, lb.floor_pct),
        COALESCE(c.cap_pct, cb.cap_pct, m.cap_pct, lb.cap_pct),
        COALESCE(c.variable_rate_type, cb.variable_rate_type, lb.variable_rate_type),
        COALESCE(c.fixing_day, cb.fixing_day, lb.fixing_day),
        CASE WHEN c.secid IS NOT NULL THEN 'curated'
             WHEN cb.secid IS NOT NULL OR m.secid IS NOT NULL OR lb.secid IS NOT NULL THEN
                  CASE WHEN COALESCE(cb.status, m.status, lb.status) = 'inferred' THEN 'inferred'
                       ELSE 'partial' END
        END,
        array_remove(ARRAY[
            CASE WHEN c.secid IS NOT NULL THEN 'curated' END,
            CASE WHEN cb.secid IS NOT NULL THEN 'cbonds' END,
            CASE WHEN m.secid IS NOT NULL THEN 'moex_iss' END,
            CASE WHEN lb.secid IS NOT NULL THEN 'legacy_bc' END,
            CASE WHEN c.benchmark_tenor_years IS NULL AND zf.benchmark_tenor_years IS NOT NULL
                 THEN 'zcyc_fit' END], NULL),
        CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.benchmark_tenor_years
             ELSE zf.benchmark_tenor_years END,
        CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.fixing_anchor
             ELSE zf.fixing_anchor END,
        CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.fixing_offset
             ELSE zf.fixing_offset END,
        CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.fixing_offset_unit
             ELSE zf.fixing_offset_unit END,
        CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN c.rate_rounding_decimals
             ELSE zf.rate_rounding_decimals END,
        CASE WHEN c.benchmark_tenor_years IS NOT NULL THEN 'document'
             WHEN zf.benchmark_tenor_years IS NOT NULL THEN 'coupon_history_fit' END,
        COALESCE(c.spread_schedule, zf.spread_schedule),
        zf.reproduction
    FROM b
    LEFT JOIN f m  ON m.secid = b.secid AND m.source = 'moex_iss'
    LEFT JOIN f cb ON cb.secid = b.secid AND cb.source = 'cbonds'
    LEFT JOIN f c  ON c.secid = b.secid AND c.source = 'curated'
    LEFT JOIN f lb ON lb.secid = b.secid AND lb.source = 'legacy_bc'
    LEFT JOIN f zf ON zf.secid = b.secid AND zf.source = 'zcyc_fit'
    WHERE m.secid IS NOT NULL OR cb.secid IS NOT NULL OR c.secid IS NOT NULL OR lb.secid IS NOT NULL
$$;
COMMENT ON FUNCTION ref.bond_rate_formula_resolved_known_at(timestamptz) IS
    'ref.bond_rate_formula_resolved as known at p_known_at (same precedence, as-known-at images), '
    'plus benchmark_raw / benchmark_source of the source that supplied the benchmark';

-- Curated margin steps (job bond_terms_curated, term floater_spread, term_key = effective_from):
-- a margin revised at an offer is announced in its own document, so every step has evidence.
ALTER TABLE ref.bond_curated_term DROP CONSTRAINT bond_curated_term_term_check;
ALTER TABLE ref.bond_curated_term ADD CONSTRAINT bond_curated_term_term_check CHECK (term IN (
    'floater_formula', 'floater_spread', 'call', 'offer', 'replacement_bond'));
