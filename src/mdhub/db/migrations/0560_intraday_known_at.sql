-- 0560_intraday_known_at: as-known-at readers of the intraday tables (read API /v1/intraday/*,
-- known_at=). Like ops.create_known_at_function (0500), but the range is an instant range on a
-- timestamptz key column [p_from, p_to), so a read of one day scans that day only.
-- md.futures_trade is append-only (no row history): its readers filter ingested_at <= known_at in
-- the API directly.

CREATE OR REPLACE PROCEDURE ops.create_known_at_ts_function(
    p_func text,
    p_tbl regclass,
    p_key_cols text[],
    p_ts_col text
)
LANGUAGE plpgsql AS $$
DECLARE
    tname   text;
    keyexpr text;
    c       text;
BEGIN
    SELECT format('%I.%I', n.nspname, cl.relname) INTO tname
    FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE cl.oid = p_tbl;
    FOREACH c IN ARRAY p_key_cols || ARRAY['ingested_at']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = p_tbl AND attname = c AND attnum > 0 AND NOT attisdropped
        ) THEN
            RAISE EXCEPTION 'ops.create_known_at_ts_function(%): column % does not exist', tname, c;
        END IF;
    END LOOP;
    IF NOT p_ts_col = ANY(p_key_cols) THEN
        RAISE EXCEPTION 'ops.create_known_at_ts_function(%): % is not a key column', tname, p_ts_col;
    END IF;
    SELECT 'jsonb_build_object(' || string_agg(format('%L, t.%I', k, k), ', ' ORDER BY o) || ')'
    INTO keyexpr FROM unnest(p_key_cols) WITH ORDINALITY AS u(k, o);
    EXECUTE format($f$
        CREATE OR REPLACE FUNCTION %1$s(p_known_at timestamptz, p_from timestamptz, p_to timestamptz)
        RETURNS SETOF %2$s
        LANGUAGE sql STABLE AS $body$
            WITH cur AS (
                SELECT t AS r, %3$s AS rk FROM %2$s t
                WHERE t.%4$I >= p_from AND t.%4$I < p_to
            ), h AS (
                SELECT DISTINCT ON (rh.row_key) rh.row_key AS rk, rh.old_row
                FROM ops.row_history rh
                WHERE rh.table_name = %5$L AND rh.replaced_at > p_known_at
                  AND (rh.row_key ->> %6$L)::timestamptz >= p_from
                  AND (rh.row_key ->> %6$L)::timestamptz < p_to
                ORDER BY rh.row_key, rh.replaced_at, rh.id
            ), img AS MATERIALIZED (
                SELECT CASE WHEN h.rk IS NOT NULL
                            THEN jsonb_populate_record(NULL::%2$s, h.old_row)
                            ELSE cur.r END AS r
                FROM cur FULL JOIN h ON h.rk = cur.rk
            )
            SELECT (img.r).* FROM img WHERE (img.r).ingested_at <= p_known_at
        $body$
    $f$, p_func, tname, keyexpr, p_ts_col, tname, p_ts_col);
    EXECUTE format('COMMENT ON FUNCTION %s(timestamptz, timestamptz, timestamptz) IS %L', p_func,
        format('%s rows as known at p_known_at (ops.row_history; version time ingested_at); %s in [p_from, p_to)',
               tname, p_ts_col));
END;
$$;
COMMENT ON PROCEDURE ops.create_known_at_ts_function(text, regclass, text[], text) IS
    'Create an as-known-at reader <func>(known_at, from, to) over ops.row_history for a table keyed by an instant';

CALL ops.create_known_at_ts_function('md.futures_intraday_known_at', 'md.futures_intraday',
                                     ARRAY['secid', 'source', 'source_variant', 'ts'], 'ts');
CALL ops.create_known_at_ts_function('md.bond_quote_intraday_known_at', 'md.bond_quote_intraday',
                                     ARRAY['secid', 'board', 'ts'], 'ts');
CALL ops.create_known_at_ts_function('md.zcyc_security_quote_known_at', 'md.zcyc_security_quote',
                                     ARRAY['secid', 'ts'], 'ts');
CALL ops.create_known_at_ts_function('md.index_intraday_known_at', 'md.index_intraday',
                                     ARRAY['secid', 'ts'], 'ts');
CALL ops.create_known_at_ts_function('md.index_candle_known_at', 'md.index_candle',
                                     ARRAY['secid', 'interval_min', 'begin_ts'], 'begin_ts');
CALL ops.create_known_at_ts_function('md.repo_intraday_known_at', 'md.repo_intraday',
                                     ARRAY['market', 'board', 'secid', 'ts'], 'ts');
-- date-keyed tables: the generic reader of 0500
CALL ops.create_known_at_function('md.index_constituent_known_at', 'md.index_constituent',
                                  ARRAY['index_id', 'trade_date', 'secid'], ARRAY['ingested_at'],
                                  'trade_date');
CALL ops.create_known_at_function('md.repo_eod_known_at', 'md.repo_eod',
                                  ARRAY['market', 'trade_date', 'board', 'secid'],
                                  ARRAY['ingested_at'], 'trade_date');
CALL ops.create_known_at_function('md.bond_close_quote_known_at', 'md.bond_close_quote',
                                  ARRAY['trade_date', 'secid', 'board', 'basis'],
                                  ARRAY['ingested_at'], 'trade_date');
