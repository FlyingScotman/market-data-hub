-- 0420_review_fixes: fixes from the 2026-10-16 correctness review (F4, F6, F8, F9, F12 and the
-- formula/liquidity follow-ups). Only new objects / replaced views / altered constraints:
-- earlier migrations stay untouched.

-- ---------------------------------------------------------------- F4 calendar precedence
-- Source rank per (calendar, day), lower wins: observed trading/fixings > days listed by the
-- official MOEX ISS calendar > curated file > unverified curated file > anything else.
-- known_at (recency) only breaks ties inside a rank. Mirrors mdhub.loaders.calendar.SOURCE_PRIORITY.
CREATE OR REPLACE FUNCTION ref.calendar_source_rank(source text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN source LIKE 'observed\_%' THEN 1
        WHEN source = 'moex_iss_calendar' THEN 2
        WHEN source = 'curated' THEN 3
        WHEN source = 'curated_unverified' THEN 4
        ELSE 9
    END
$$;
COMMENT ON FUNCTION ref.calendar_source_rank(text) IS
    'Precedence of ref.calendar_day sources (1 = highest): observed_* > moex_iss_calendar > curated > curated_unverified > other';

CREATE OR REPLACE VIEW ref.calendar_effective AS
SELECT DISTINCT ON (calendar, day) calendar, day, is_business_day, source, known_at, version
FROM ref.calendar_day
ORDER BY calendar, day, ref.calendar_source_rank(source), known_at DESC;
COMMENT ON VIEW ref.calendar_effective IS
    'Business-day flag per (calendar, day) from the highest-priority source '
    '(ref.calendar_source_rank: observed > MOEX ISS calendar rows > curated > curated_unverified; '
    'known_at breaks ties). Days without any row: weekday rule in mdhub.core.bizday';
COMMENT ON TABLE ref.calendar_day IS
    'Business days per calendar and source; consumers take, per (calendar, day), the row of the '
    'highest-priority source (ref.calendar_source_rank; ref.calendar_effective, '
    'mdhub.core.bizday.DbCalendar). Sources: observed_* (past days from MOEX trading / CBR RUONIA '
    'observations, from 2019), moex_iss_calendar (only the days the official MOEX ISS calendar '
    'lists), curated / curated_unverified (versioned file mdhub/data/calendar_curated.json).';

-- ------------------------------------------------- F9 floater current coupon: new status
DO $$
DECLARE
    c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
        WHERE con.conrelid = 'ref.bond_coupon'::regclass AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%projected_by_moex%'
    LOOP
        EXECUTE format('ALTER TABLE ref.bond_coupon DROP CONSTRAINT %I', c);
    END LOOP;
END;
$$;
ALTER TABLE ref.bond_coupon ADD CONSTRAINT bond_coupon_status_chk
    CHECK (status IN ('known', 'current_published', 'projected_by_moex', 'unknown'));
COMMENT ON COLUMN ref.bond_coupon.status IS
    'known: fixed published amount/rate, or a paid coupon; current_published: the current period '
    'of a floater as MOEX publishes it, not proven to be fixed in advance (may be a projection, '
    'e.g. an average rate paid in arrears) - becomes known once paid or when a curated formula '
    'states a fixing before the period; projected_by_moex: MOEX value that is not a fixed amount '
    '(floating coupons beyond the current period, in-arrears benchmarks, amounts on an indexed '
    'face, bonds of unknown coupon type); unknown: no value published (amount/rate NULL)';
COMMENT ON COLUMN ref.bond.coupon_type_basis IS
    'moex_label | benchmark | ofz_series | schedule (matured bond: fixed from its paid schedule; '
    'active unlabelled bond with a fully valued schedule: coupon_type unknown) | none';

-- -------------------------------------------- F6 intraday same-ts updates keep history
CALL ops.enable_history('md.fx_intraday', ARRAY['secid', 'board', 'source', 'source_variant', 'ts']);
CALL ops.enable_history('md.futures_intraday', ARRAY['secid', 'source', 'source_variant', 'ts']);

-- ------------------------------------------- F8 bond intraday captures under retention
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT con.conname, cl.relname
        FROM pg_constraint con
        JOIN pg_class cl ON cl.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE con.contype = 'f' AND n.nspname = 'md'
          AND cl.relname IN ('bond_quote_intraday', 'bond_quote_last')
          AND con.confrelid = 'ops.raw_capture'::regclass
    LOOP
        EXECUTE format('ALTER TABLE md.%I DROP CONSTRAINT %I', r.relname, r.conname);
    END LOOP;
END;
$$;
ALTER TABLE md.bond_quote_intraday ADD CONSTRAINT bond_quote_intraday_capture_fk
    FOREIGN KEY (capture_id) REFERENCES ops.raw_capture (id) ON DELETE SET NULL;
ALTER TABLE md.bond_quote_last ADD CONSTRAINT bond_quote_last_capture_fk
    FOREIGN KEY (capture_id) REFERENCES ops.raw_capture (id) ON DELETE SET NULL;
UPDATE ops.raw_capture SET endpoint = 'intraday:bonds_marketdata'
WHERE source = 'moex_iss' AND endpoint = 'bonds_marketdata';

-- --------------------------------------- floater formula: legacy rows at lowest priority
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
        CASE WHEN lb.secid IS NOT NULL THEN 'legacy_bc' END], NULL)            AS sources
FROM ref.bond b
LEFT JOIN ref.bond_rate_formula m  ON m.secid = b.secid AND m.source = 'moex_iss' AND NOT m.is_deleted
LEFT JOIN ref.bond_rate_formula cb ON cb.secid = b.secid AND cb.source = 'cbonds' AND NOT cb.is_deleted
LEFT JOIN ref.bond_rate_formula c  ON c.secid = b.secid AND c.source = 'curated' AND NOT c.is_deleted
LEFT JOIN ref.bond_rate_formula lb ON lb.secid = b.secid AND lb.source = 'legacy_bc' AND NOT lb.is_deleted
WHERE m.secid IS NOT NULL OR cb.secid IS NOT NULL OR c.secid IS NOT NULL OR lb.secid IS NOT NULL;
COMMENT ON VIEW ref.bond_rate_formula_resolved IS
    'Field-level precedence curated > cbonds > moex_iss > legacy_bc for conventions; benchmark '
    'curated > moex_iss > cbonds > legacy_bc (legacy_bc = MOEX labels as of the legacy snapshot)';

-- ------------------------------------------------ CBR liquidity: column labels verified
COMMENT ON COLUMN md.cbr_liquidity.secured_loans_auction_bn IS
    'Column 6 of /hd_base/bliquidity/: standard instruments, claims, auctions - secured loans '
    '(header checked live 2026-10-16; the legacy schema labels columns 6/7 the other way round)';
COMMENT ON COLUMN md.cbr_liquidity.repo_fxswap_standing_bn IS
    'Column 7 of /hd_base/bliquidity/: standard instruments, claims, standing facilities - repo '
    'and FX swap (header checked live 2026-10-16)';

-- ------------------------------------------------------- F12 tasks shared per backfill job
-- A task of any dataset sharing the backfill job (ops.dataset_status.backfill_job) covers a gap.
CREATE OR REPLACE VIEW ops.v_coverage AS
SELECT
    s.dataset,
    s.table_name,
    s.frequency,
    s.calendar,
    s.first_date,
    s.last_date,
    s.row_count,
    s.expected_latest,
    s.is_stale,
    (SELECT count(*) FROM ops.data_gap g
      WHERE g.dataset = s.dataset AND g.status = 'open') AS open_gaps,
    (SELECT count(*) FROM ops.data_gap g
      WHERE g.dataset = s.dataset AND g.status = 'unfillable') AS unfillable_gaps,
    (SELECT count(*) FROM ops.backfill_task t
      WHERE t.status IN ('pending', 'running')
        AND (t.dataset = s.dataset
             OR t.dataset IN (SELECT s2.dataset FROM ops.dataset_status s2
                              WHERE s2.backfill_job = s.backfill_job))) AS open_tasks,
    s.backfill_job,
    lr.status AS last_run_status,
    lr.started_at AS last_run_at,
    ls.last_success_at,
    s.problem,
    s.checked_at
FROM ops.dataset_status s
LEFT JOIN LATERAL (
    SELECT r.status, r.started_at FROM ops.job_run r
    WHERE r.job = s.backfill_job
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT 1
) lr ON true
LEFT JOIN LATERAL (
    SELECT max(r.started_at) AS last_success_at FROM ops.job_run r
    WHERE r.job = s.backfill_job AND r.status IN ('succeeded', 'partial')
) ls ON true;
COMMENT ON VIEW ops.v_coverage IS
    'Per dataset: first/last date, rows, staleness, gaps, open tasks of its backfill job and last run of that job';

CREATE OR REPLACE VIEW ops.v_open_gaps AS
SELECT
    g.dataset,
    g.key,
    g.gap_date,
    g.detected_at,
    g.note,
    t.id AS task_id,
    t.status AS task_status,
    t.attempts AS task_attempts,
    t.not_before AS task_not_before
FROM ops.data_gap g
LEFT JOIN LATERAL (
    SELECT bt.id, bt.status, bt.attempts, bt.not_before FROM ops.backfill_task bt
    WHERE (bt.dataset = g.dataset
           OR bt.dataset IN (SELECT s2.dataset FROM ops.dataset_status s1
                             JOIN ops.dataset_status s2 ON s2.backfill_job = s1.backfill_job
                             WHERE s1.dataset = g.dataset))
      AND bt.params ? 'date_from' AND bt.params ? 'date_to'
      AND (bt.params ->> 'date_from') <= g.gap_date::text
      AND (bt.params ->> 'date_to') >= g.gap_date::text
    ORDER BY bt.created_at DESC, bt.id DESC
    LIMIT 1
) t ON true
WHERE g.status = 'open';
COMMENT ON VIEW ops.v_open_gaps IS
    'Open data gaps with the latest backfill task covering each day (task of the dataset or of a '
    'dataset sharing its backfill job; ISO date params)';

-- ------------------------------------------------------------ F2 lock-busy skipped runs
COMMENT ON COLUMN ops.job_run.error IS
    'Sanitized error text (failed) or reasons (partial/skipped). A scheduled run that could not '
    'get its job lock within the wait is skipped with error ''lock_busy: ...'' (job health: missed; '
    'the quality job re-runs it)';
