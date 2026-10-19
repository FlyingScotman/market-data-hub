-- 0500_known_at_readers: as-known-at readers for the static tables (ref.*) and the market
-- tables a published snapshot reads (Market Snapshot Contract v1, DESIGN §4/§11), and
-- versioned (soft) deletes for the two calendar tables that were hard-deleted on refresh.
-- Number range 0500+: snapshot dispatch layer. Only new objects, added columns and replaced
-- views; earlier migrations stay untouched. Safe on a populated database.
--
-- As-known-at rule (same as md.bond_eod_known_at, 0410): the image of a key known at T is the
--   OLD image of the earliest ops.row_history row of that key with replaced_at > T (that
--   version was current at T), else the current row; it is returned only if the image's
--   version time <= T (the version existed at T). Version time = ingested_at (first insert,
--   or the start of the current version where a loader refreshes it on change: md.fixing,
--   md.fx_official, ref.calendar_day, ref.weekend_session).
-- Rows are never deleted from the tables read here (soft deletes: is_deleted), so a key
-- without a current row only appears through history (pre-0500 calendar hard deletes left
-- their image with '_deleted' in ops.row_history and replay correctly through the rule).

-- ------------------------------------------------------------ row_history lookup by time
CREATE INDEX IF NOT EXISTS row_history_table_time_idx
    ON ops.row_history (table_name, replaced_at);

-- ------------------------------------------------- calendars: soft deletes + version time
-- ingested_at = when the current version of the row was written (refreshed on change, like
-- md.fixing); known_at keeps its meaning (when the flag became known; curated rows carry the
-- curated file's known_at, which is earlier than the hub learned it). Existing rows: the
-- later of known_at and the start of the run that wrote the version.
ALTER TABLE ref.calendar_day ADD COLUMN IF NOT EXISTS is_deleted boolean NOT NULL DEFAULT false;
ALTER TABLE ref.calendar_day ADD COLUMN IF NOT EXISTS ingested_at timestamptz;
UPDATE ref.calendar_day c
SET ingested_at = GREATEST(c.known_at, COALESCE(
    (SELECT r.started_at FROM ops.job_run r WHERE r.id = c.run_id), c.known_at))
WHERE c.ingested_at IS NULL;
ALTER TABLE ref.calendar_day ALTER COLUMN ingested_at SET DEFAULT now();
ALTER TABLE ref.calendar_day ALTER COLUMN ingested_at SET NOT NULL;
COMMENT ON COLUMN ref.calendar_day.is_deleted IS
    'true: the source no longer lists the day (MOEX ISS calendar repair); a versioned change '
    '(old image in ops.row_history), never a DELETE. Readers skip deleted rows';
COMMENT ON COLUMN ref.calendar_day.ingested_at IS
    'Version time: when the hub wrote the current version of the row (insert or change)';

ALTER TABLE ref.weekend_session ADD COLUMN IF NOT EXISTS is_deleted boolean NOT NULL DEFAULT false;
ALTER TABLE ref.weekend_session ADD COLUMN IF NOT EXISTS ingested_at timestamptz;
UPDATE ref.weekend_session s
SET ingested_at = GREATEST(s.known_at, COALESCE(
    (SELECT r.started_at FROM ops.job_run r WHERE r.id = s.run_id), s.known_at))
WHERE s.ingested_at IS NULL;
ALTER TABLE ref.weekend_session ALTER COLUMN ingested_at SET DEFAULT now();
ALTER TABLE ref.weekend_session ALTER COLUMN ingested_at SET NOT NULL;
COMMENT ON COLUMN ref.weekend_session.is_deleted IS
    'true: MOEX ISS no longer lists the future session; a versioned change (old image in '
    'ops.row_history), never a DELETE. Readers skip deleted rows';
COMMENT ON COLUMN ref.weekend_session.ingested_at IS
    'Version time: when the hub wrote the current version of the row (insert or change)';

CREATE OR REPLACE VIEW ref.calendar_effective AS
SELECT DISTINCT ON (calendar, day) calendar, day, is_business_day, source, known_at, version
FROM ref.calendar_day
WHERE NOT is_deleted
ORDER BY calendar, day, ref.calendar_source_rank(source), known_at DESC;
COMMENT ON VIEW ref.calendar_effective IS
    'Business-day flag per (calendar, day) from the highest-priority non-deleted source '
    '(ref.calendar_source_rank: observed > MOEX ISS calendar rows > curated > rule > '
    'curated_unverified; known_at breaks ties). Days without any row: weekday rule in '
    'mdhub.core.bizday. As known at T: ref.calendar_effective_known_at(T)';

-- -------------------------------------------------------------- reader generator
-- Creates <p_func>(p_known_at timestamptz [, p_from date, p_to date]) RETURNS SETOF <p_tbl>
-- implementing the as-known-at rule above. p_key_cols must be the key columns given to
-- ops.enable_history for the table (they build ops.row_history.row_key). p_version_cols:
-- the version time is the first non-NULL of these columns of the image (history images
-- written before a column existed lack it). p_date_col (a key column): optional business
-- date filter [p_from, p_to] on the current rows and on the history row keys.
CREATE OR REPLACE PROCEDURE ops.create_known_at_function(
    p_func text,
    p_tbl regclass,
    p_key_cols text[],
    p_version_cols text[] DEFAULT ARRAY['ingested_at'],
    p_date_col text DEFAULT NULL
)
LANGUAGE plpgsql AS $$
DECLARE
    tname   text;
    keyexpr text;
    verexpr text;
    params  text;
    curflt  text := '';
    histflt text := '';
    c       text;
BEGIN
    SELECT format('%I.%I', n.nspname, cl.relname) INTO tname
    FROM pg_class cl JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE cl.oid = p_tbl;
    FOREACH c IN ARRAY p_key_cols || p_version_cols
                       || CASE WHEN p_date_col IS NULL THEN ARRAY[]::text[] ELSE ARRAY[p_date_col] END
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = p_tbl AND attname = c AND attnum > 0 AND NOT attisdropped
        ) THEN
            RAISE EXCEPTION 'ops.create_known_at_function(%): column % does not exist', tname, c;
        END IF;
    END LOOP;
    IF p_date_col IS NOT NULL AND NOT p_date_col = ANY(p_key_cols) THEN
        RAISE EXCEPTION 'ops.create_known_at_function(%): % is not a key column', tname, p_date_col;
    END IF;
    SELECT 'jsonb_build_object(' || string_agg(format('%L, t.%I', k, k), ', ' ORDER BY o) || ')'
    INTO keyexpr FROM unnest(p_key_cols) WITH ORDINALITY AS u(k, o);
    SELECT CASE WHEN count(*) = 1 THEN max(format('(img.r).%I', v))
                ELSE 'COALESCE(' || string_agg(format('(img.r).%I', v), ', ' ORDER BY o) || ')' END
    INTO verexpr FROM unnest(p_version_cols) WITH ORDINALITY AS u(v, o);
    params := 'p_known_at timestamptz';
    IF p_date_col IS NOT NULL THEN
        params := params || ', p_from date, p_to date';
        curflt := format(' WHERE t.%I BETWEEN p_from AND p_to', p_date_col);
        histflt := format(
            ' AND rh.row_key ->> %L BETWEEN to_char(p_from, ''YYYY-MM-DD'') '
            'AND to_char(p_to, ''YYYY-MM-DD'')', p_date_col);
    END IF;
    EXECUTE format($f$
        CREATE OR REPLACE FUNCTION %1$s(%2$s) RETURNS SETOF %3$s
        LANGUAGE sql STABLE AS $body$
            WITH cur AS (
                SELECT t AS r, %4$s AS rk FROM %3$s t%5$s
            ), h AS (
                SELECT DISTINCT ON (rh.row_key) rh.row_key AS rk, rh.old_row
                FROM ops.row_history rh
                WHERE rh.table_name = %6$L AND rh.replaced_at > p_known_at%7$s
                ORDER BY rh.row_key, rh.replaced_at, rh.id
            ), img AS MATERIALIZED (
                SELECT CASE WHEN h.rk IS NOT NULL
                            THEN jsonb_populate_record(NULL::%3$s, h.old_row)
                            ELSE cur.r END AS r
                FROM cur FULL JOIN h ON h.rk = cur.rk
            )
            SELECT (img.r).* FROM img WHERE %8$s <= p_known_at
        $body$
    $f$, p_func, params, tname, keyexpr, curflt, tname, histflt, verexpr);
    EXECUTE format('COMMENT ON FUNCTION %s(%s) IS %L', p_func,
        CASE WHEN p_date_col IS NULL THEN 'timestamptz' ELSE 'timestamptz, date, date' END,
        format('%s rows as known at p_known_at (ops.row_history; version time %s)%s', tname,
               array_to_string(p_version_cols, ' / '),
               CASE WHEN p_date_col IS NULL THEN ''
                    ELSE format('; %s in [p_from, p_to]', p_date_col) END));
END;
$$;
COMMENT ON PROCEDURE ops.create_known_at_function(text, regclass, text[], text[], text) IS
    'Create an as-known-at reader <func>(known_at [, from, to]) over ops.row_history for a table';

-- ---------------------------------------------------------------------- ref readers
CALL ops.create_known_at_function('ref.issuer_known_at', 'ref.issuer', ARRAY['issuer_id']);
CALL ops.create_known_at_function('ref.bond_known_at', 'ref.bond', ARRAY['secid']);
CALL ops.create_known_at_function('ref.bond_board_known_at', 'ref.bond_board',
                                  ARRAY['secid', 'board']);
CALL ops.create_known_at_function('ref.bond_coupon_known_at', 'ref.bond_coupon',
                                  ARRAY['secid', 'coupon_no']);
CALL ops.create_known_at_function('ref.bond_amortization_known_at', 'ref.bond_amortization',
                                  ARRAY['secid', 'amort_date', 'data_source']);
CALL ops.create_known_at_function('ref.bond_offer_known_at', 'ref.bond_offer',
                                  ARRAY['secid', 'source', 'offer_key']);
CALL ops.create_known_at_function('ref.bond_rate_formula_known_at', 'ref.bond_rate_formula',
                                  ARRAY['secid', 'source']);
CALL ops.create_known_at_function('ref.fx_instrument_known_at', 'ref.fx_instrument',
                                  ARRAY['secid', 'board']);
CALL ops.create_known_at_function('ref.cbr_meeting_known_at', 'ref.cbr_meeting',
                                  ARRAY['meeting_date']);
CALL ops.create_known_at_function('ref.calendar_day_known_at', 'ref.calendar_day',
                                  ARRAY['calendar', 'day', 'source'],
                                  ARRAY['ingested_at', 'known_at']);
CALL ops.create_known_at_function('ref.weekend_session_known_at', 'ref.weekend_session',
                                  ARRAY['calendar', 'day', 'source'],
                                  ARRAY['ingested_at', 'known_at']);

-- Same precedence as ref.bond_rate_formula_resolved (0420), over the as-known-at images.
CREATE OR REPLACE FUNCTION ref.bond_rate_formula_resolved_known_at(p_known_at timestamptz)
RETURNS TABLE (
    secid text, benchmark text, benchmark_raw text, benchmark_source text, spread_bp numeric, multiplier numeric, lag_days integer,
    averaging text, floor_pct numeric, cap_pct numeric, variable_rate_type text,
    fixing_day integer, status text, sources text[]
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
            CASE WHEN lb.secid IS NOT NULL THEN 'legacy_bc' END], NULL)
    FROM b
    LEFT JOIN f m  ON m.secid = b.secid AND m.source = 'moex_iss'
    LEFT JOIN f cb ON cb.secid = b.secid AND cb.source = 'cbonds'
    LEFT JOIN f c  ON c.secid = b.secid AND c.source = 'curated'
    LEFT JOIN f lb ON lb.secid = b.secid AND lb.source = 'legacy_bc'
    WHERE m.secid IS NOT NULL OR cb.secid IS NOT NULL OR c.secid IS NOT NULL OR lb.secid IS NOT NULL
$$;
COMMENT ON FUNCTION ref.bond_rate_formula_resolved_known_at(timestamptz) IS
    'ref.bond_rate_formula_resolved as known at p_known_at (same precedence, as-known-at images), '
    'plus benchmark_raw / benchmark_source of the source that supplied the benchmark';

-- Same rule as ref.calendar_effective, over the as-known-at images.
CREATE OR REPLACE FUNCTION ref.calendar_effective_known_at(p_known_at timestamptz)
RETURNS TABLE (
    calendar text, day date, is_business_day boolean, source text, known_at timestamptz,
    version text, run_id bigint, ingested_at timestamptz
)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (c.calendar, c.day)
           c.calendar, c.day, c.is_business_day, c.source, c.known_at, c.version, c.run_id,
           COALESCE(c.ingested_at, c.known_at)
    FROM ref.calendar_day_known_at(p_known_at) c
    WHERE NOT COALESCE(c.is_deleted, false)
    ORDER BY c.calendar, c.day, ref.calendar_source_rank(c.source), c.known_at DESC
$$;
COMMENT ON FUNCTION ref.calendar_effective_known_at(timestamptz) IS
    'ref.calendar_effective as known at p_known_at (never reads ref.weekend_session)';

-- ------------------------------------------------------------------- md readers (dated)
CALL ops.create_known_at_function('md.fx_official_known_at', 'md.fx_official',
                                  ARRAY['effective_date', 'ccy'], ARRAY['ingested_at'],
                                  'effective_date');
CALL ops.create_known_at_function('md.fx_eod_known_at', 'md.fx_eod',
                                  ARRAY['trade_date', 'secid', 'board'], ARRAY['ingested_at'],
                                  'trade_date');
CALL ops.create_known_at_function('md.fx_indicative_known_at', 'md.fx_indicative',
                                  ARRAY['trade_date', 'clearing', 'ccy_pair'],
                                  ARRAY['ingested_at'], 'trade_date');
CALL ops.create_known_at_function('md.ecb_fx_known_at', 'md.ecb_fx',
                                  ARRAY['rate_date', 'ccy'], ARRAY['ingested_at'], 'rate_date');
CALL ops.create_known_at_function('md.index_eod_known_at', 'md.index_eod',
                                  ARRAY['trade_date', 'secid'], ARRAY['ingested_at'],
                                  'trade_date');
CALL ops.create_known_at_function('md.zcyc_snapshot_known_at', 'md.zcyc_snapshot',
                                  ARRAY['trade_date', 'trade_time'], ARRAY['ingested_at'],
                                  'trade_date');
