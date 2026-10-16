-- 0410_api_views: views and "as known at" functions used by the read API (mdhub.api) and
-- the TDM export (mdhub.export.tdm). DESIGN §8.
--
-- As-known-at reconstruction (bitemporal read) for tables with ops.row_history:
--   the image of a key known at T is
--     * the OLD image of the earliest ops.row_history row of that key with replaced_at > T
--       (that version was current at T), else the current row;
--     * and only if that image's ingested_at <= T (the version existed at T).
--   Loaders never delete rows, so keys without a current row only appear through history.
--   ingested_at is the first-insert time (md.bond_eod*) or the known-at of the current
--   version (md.fixing refreshes it on change); both satisfy the rule above.

-- History lookups by business date (row_key dates are ISO text: to_jsonb(date)).
CREATE INDEX row_history_trade_date_idx
    ON ops.row_history (table_name, (row_key ->> 'trade_date'), replaced_at)
    WHERE table_name IN ('md.bond_eod', 'md.bond_eod_yield');
CREATE INDEX row_history_fixing_date_idx
    ON ops.row_history (table_name, (row_key ->> 'fixing_date'), replaced_at)
    WHERE table_name = 'md.fixing';

-- ---------------------------------------------------------------- md.bond_eod_known_at
CREATE FUNCTION md.bond_eod_known_at(p_known_at timestamptz, p_from date, p_to date)
RETURNS SETOF md.bond_eod
LANGUAGE sql STABLE AS $$
    WITH cur AS (
        SELECT t AS r,
               jsonb_build_object('trade_date', t.trade_date, 'secid', t.secid, 'board', t.board) AS rk
        FROM md.bond_eod t
        WHERE t.trade_date BETWEEN p_from AND p_to
    ), h AS (
        SELECT DISTINCT ON (rh.row_key) rh.row_key AS rk, rh.old_row
        FROM ops.row_history rh
        WHERE rh.table_name = 'md.bond_eod'
          AND rh.replaced_at > p_known_at
          AND rh.row_key ->> 'trade_date' BETWEEN to_char(p_from, 'YYYY-MM-DD') AND to_char(p_to, 'YYYY-MM-DD')
        ORDER BY rh.row_key, rh.replaced_at, rh.id
    ), img AS MATERIALIZED (
        SELECT CASE WHEN h.rk IS NOT NULL
                    THEN jsonb_populate_record(NULL::md.bond_eod, h.old_row)
                    ELSE cur.r END AS r
        FROM cur FULL JOIN h ON h.rk = cur.rk
    )
    SELECT (img.r).* FROM img WHERE (img.r).ingested_at <= p_known_at
$$;
COMMENT ON FUNCTION md.bond_eod_known_at(timestamptz, date, date) IS
    'md.bond_eod rows of trade dates [p_from, p_to] as known at p_known_at (ops.row_history)';

-- ---------------------------------------------------------- md.bond_eod_yield_known_at
CREATE FUNCTION md.bond_eod_yield_known_at(p_known_at timestamptz, p_from date, p_to date)
RETURNS SETOF md.bond_eod_yield
LANGUAGE sql STABLE AS $$
    WITH cur AS (
        SELECT t AS r,
               jsonb_build_object('trade_date', t.trade_date, 'secid', t.secid, 'board', t.board) AS rk
        FROM md.bond_eod_yield t
        WHERE t.trade_date BETWEEN p_from AND p_to
    ), h AS (
        SELECT DISTINCT ON (rh.row_key) rh.row_key AS rk, rh.old_row
        FROM ops.row_history rh
        WHERE rh.table_name = 'md.bond_eod_yield'
          AND rh.replaced_at > p_known_at
          AND rh.row_key ->> 'trade_date' BETWEEN to_char(p_from, 'YYYY-MM-DD') AND to_char(p_to, 'YYYY-MM-DD')
        ORDER BY rh.row_key, rh.replaced_at, rh.id
    ), img AS MATERIALIZED (
        SELECT CASE WHEN h.rk IS NOT NULL
                    THEN jsonb_populate_record(NULL::md.bond_eod_yield, h.old_row)
                    ELSE cur.r END AS r
        FROM cur FULL JOIN h ON h.rk = cur.rk
    )
    SELECT (img.r).* FROM img WHERE (img.r).ingested_at <= p_known_at
$$;
COMMENT ON FUNCTION md.bond_eod_yield_known_at(timestamptz, date, date) IS
    'md.bond_eod_yield rows of trade dates [p_from, p_to] as known at p_known_at (ops.row_history)';

-- ------------------------------------------------------------------ md.fixing_known_at
CREATE FUNCTION md.fixing_known_at(p_known_at timestamptz, p_from date, p_to date)
RETURNS SETOF md.fixing
LANGUAGE sql STABLE AS $$
    WITH cur AS (
        SELECT t AS r,
               jsonb_build_object('index_code', t.index_code, 'fixing_date', t.fixing_date,
                                  'source', t.source) AS rk
        FROM md.fixing t
        WHERE t.fixing_date BETWEEN p_from AND p_to
    ), h AS (
        SELECT DISTINCT ON (rh.row_key) rh.row_key AS rk, rh.old_row
        FROM ops.row_history rh
        WHERE rh.table_name = 'md.fixing'
          AND rh.replaced_at > p_known_at
          AND rh.row_key ->> 'fixing_date' BETWEEN to_char(p_from, 'YYYY-MM-DD') AND to_char(p_to, 'YYYY-MM-DD')
        ORDER BY rh.row_key, rh.replaced_at, rh.id
    ), img AS MATERIALIZED (
        SELECT CASE WHEN h.rk IS NOT NULL
                    THEN jsonb_populate_record(NULL::md.fixing, h.old_row)
                    ELSE cur.r END AS r
        FROM cur FULL JOIN h ON h.rk = cur.rk
    )
    SELECT (img.r).* FROM img WHERE (img.r).ingested_at <= p_known_at
$$;
COMMENT ON FUNCTION md.fixing_known_at(timestamptz, date, date) IS
    'md.fixing rows of fixing dates [p_from, p_to] as known at p_known_at (ops.row_history)';

-- ----------------------------------------------------------------------- md.v_bond_eod
-- Bonds EOD joined with the MOEX yields sibling; units in the column names:
--   *_pct  prices in % of face (as MOEX publishes them) or yields in % p.a.,
--   *_per_bond  amounts per one bond in currency_id (accrued) / face_unit (coupon),
--   value_ccy   turnover in currency_id, *_bp basis points, *_days days.
-- y_* columns come from md.bond_eod_yield (effective yield to y_yield_date of y_yield_date_type).
CREATE VIEW md.v_bond_eod AS
SELECT
    e.trade_date,
    e.secid,
    e.board,
    e.short_name,
    e.open                        AS open_pct,
    e.low                         AS low_pct,
    e.high                        AS high_pct,
    e.close                       AS close_pct,
    e.legal_close                 AS legal_close_pct,
    e.waprice                     AS waprice_pct,
    e.market_price2               AS market_price2_pct,
    e.market_price3               AS market_price3_pct,
    e.admitted_quote              AS admitted_quote_pct,
    e.accrued_int                 AS accrued_int_per_bond,
    e.face_value,
    e.face_unit,
    e.currency_id,
    e.volume                      AS volume_bonds,
    e.value                       AS value_ccy,
    e.num_trades,
    e.yield_close_pct,
    e.yield_at_waprice_pct,
    e.yield_to_offer_pct,
    e.yield_last_coupon_pct,
    e.duration_days,
    e.coupon_pct,
    e.coupon_value                AS coupon_value_per_bond,
    e.mat_date,
    e.offer_date,
    e.buyback_date,
    e.call_option_date,
    e.put_option_date,
    e.zspread_bp,
    e.zspread_at_waprice_bp,
    e.trading_session,
    e.bond_type,
    e.bond_subtype,
    y.yield_date                  AS y_yield_date,
    y.yield_date_type             AS y_yield_date_type,
    y.price                       AS y_price_pct,
    y.accrued_int                 AS y_accrued_int_per_bond,
    y.zcyc_moment                 AS y_zcyc_moment,
    y.effective_yield_pct         AS y_effective_yield_pct,
    y.duration_days               AS y_duration_days,
    y.zspread_bp                  AS y_zspread_bp,
    y.gspread_bp                  AS y_gspread_bp,
    y.waprice                     AS y_waprice_pct,
    y.effective_yield_waprice_pct AS y_effective_yield_waprice_pct,
    y.duration_waprice_days       AS y_duration_waprice_days,
    y.yield_to_offer_pct          AS y_yield_to_offer_pct,
    e.ingested_at,
    e.updated_at,
    y.ingested_at                 AS y_ingested_at,
    y.updated_at                  AS y_updated_at
FROM md.bond_eod e
LEFT JOIN md.bond_eod_yield y
       ON y.trade_date = e.trade_date AND y.secid = e.secid AND y.board = e.board;
COMMENT ON VIEW md.v_bond_eod IS
    'md.bond_eod + md.bond_eod_yield (y_*) with unit-suffixed column names (read API /v1/bonds/eod)';

-- Same projection over the as-known-at images (column list must stay identical to the view;
-- tests compare both).
CREATE FUNCTION md.v_bond_eod_known_at(p_known_at timestamptz, p_from date, p_to date)
RETURNS SETOF md.v_bond_eod
LANGUAGE sql STABLE AS $$
    SELECT
        e.trade_date, e.secid, e.board, e.short_name,
        e.open, e.low, e.high, e.close, e.legal_close, e.waprice,
        e.market_price2, e.market_price3, e.admitted_quote,
        e.accrued_int, e.face_value, e.face_unit, e.currency_id,
        e.volume, e.value, e.num_trades,
        e.yield_close_pct, e.yield_at_waprice_pct, e.yield_to_offer_pct, e.yield_last_coupon_pct,
        e.duration_days, e.coupon_pct, e.coupon_value,
        e.mat_date, e.offer_date, e.buyback_date, e.call_option_date, e.put_option_date,
        e.zspread_bp, e.zspread_at_waprice_bp, e.trading_session, e.bond_type, e.bond_subtype,
        y.yield_date, y.yield_date_type, y.price, y.accrued_int, y.zcyc_moment,
        y.effective_yield_pct, y.duration_days, y.zspread_bp, y.gspread_bp,
        y.waprice, y.effective_yield_waprice_pct, y.duration_waprice_days, y.yield_to_offer_pct,
        e.ingested_at, e.updated_at, y.ingested_at, y.updated_at
    FROM md.bond_eod_known_at(p_known_at, p_from, p_to) e
    LEFT JOIN md.bond_eod_yield_known_at(p_known_at, p_from, p_to) y
           ON y.trade_date = e.trade_date AND y.secid = e.secid AND y.board = e.board
$$;
COMMENT ON FUNCTION md.v_bond_eod_known_at(timestamptz, date, date) IS
    'md.v_bond_eod rows of trade dates [p_from, p_to] as known at p_known_at';

-- ---------------------------------------------------------------- md.v_fx_cbr_latest
CREATE VIEW md.v_fx_cbr_latest AS
SELECT DISTINCT ON (ccy)
    ccy, effective_date, set_date, record_date, nominal, value, rate_per_unit, vunit_rate, ingested_at
FROM md.fx_official
ORDER BY ccy, effective_date DESC;
COMMENT ON VIEW md.v_fx_cbr_latest IS 'Latest CBR official rate per currency (by effective date)';
