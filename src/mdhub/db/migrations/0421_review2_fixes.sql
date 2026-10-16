-- 0421_review2_fixes: fixes from the 2026-10-16 follow-up review (N1-N9). Only new objects,
-- nullable columns, replaced views / constraints and indexes: safe on a populated database and
-- compatible with code of the previous release that is still running while it is applied.

-- ------------------------------------------------------------ N3 origin of each job run
-- NULL for rows written before this migration (and by code of the previous release).
ALTER TABLE ops.job_run ADD COLUMN IF NOT EXISTS trigger text;
ALTER TABLE ops.job_run DROP CONSTRAINT IF EXISTS job_run_trigger_chk;
ALTER TABLE ops.job_run ADD CONSTRAINT job_run_trigger_chk CHECK (
    trigger IS NULL OR trigger IN ('schedule', 'catch_up', 'backfill', 'cli', 'api', 'other'));
COMMENT ON COLUMN ops.job_run.trigger IS
    'Who started the run: schedule (scheduler fire, incl. lock_busy skips), catch_up (quality '
    're-run of a fire skipped with lock_busy), backfill (backfill task run), cli (mdhub run / '
    'mdhub ops ...), api, other; NULL = recorded before 0421. Job health counts only schedule / '
    'catch_up (and NULL) runs as "the fire ran"';

-- ------------------------------------------- N6 gaps that the calendar no longer expects
DO $$
DECLARE
    c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
        WHERE con.conrelid = 'ops.data_gap'::regclass AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%unfillable%'
    LOOP
        EXECUTE format('ALTER TABLE ops.data_gap DROP CONSTRAINT %I', c);
    END LOOP;
END;
$$;
ALTER TABLE ops.data_gap ADD CONSTRAINT data_gap_status_chk
    CHECK (status IN ('open', 'backfilled', 'unfillable', 'not_expected'));
COMMENT ON COLUMN ops.data_gap.status IS
    'open: missing, to be backfilled; backfilled: data present now (loaded or published late); '
    'unfillable: the source confirmed "no data" for the day after the grace period of one business '
    'day (terminal unless data appears later); not_expected: the gap scan no longer expects data '
    'for the day (calendar corrected, e.g. a holiday) and the table has no data for it';

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
    s.checked_at,
    (SELECT count(*) FROM ops.data_gap g
      WHERE g.dataset = s.dataset AND g.status = 'not_expected') AS not_expected_gaps
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
    'Per dataset: first/last date, rows, staleness, gaps by status (open, unfillable, not_expected), '
    'open tasks of its backfill job and last run of that job';

-- ------------------------------- N2 constant-rate schedule of an unlabelled active bond
DO $$
DECLARE
    c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
        WHERE con.conrelid = 'ref.bond'::regclass AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%ofz_series%'
    LOOP
        EXECUTE format('ALTER TABLE ref.bond DROP CONSTRAINT %I', c);
    END LOOP;
END;
$$;
ALTER TABLE ref.bond ADD CONSTRAINT bond_coupon_type_basis_chk CHECK (coupon_type_basis IN (
    'moex_label', 'benchmark', 'ofz_series', 'schedule', 'schedule_constant', 'none'));
COMMENT ON COLUMN ref.bond.coupon_type_basis IS
    'moex_label | benchmark | ofz_series | schedule (matured bond: fixed from its paid schedule; '
    'active unlabelled bond with a fully valued schedule: coupon_type unknown) | schedule_constant '
    '(active unlabelled bond classified fixed: no benchmark / COUPON_DETAILS and no rate formula '
    'from any source, >= 2 paid coupons, one constant rate (value_prc) over all paid and future '
    'coupons, and the paid coupon periods span at least one CBR key-rate change, so a floater on an '
    'unchanged key rate cannot pass) | none';

-- ------------------------------------------ N1/N7 MOEX weekend (non-business) sessions
CREATE TABLE IF NOT EXISTS ref.weekend_session (
    calendar           text NOT NULL
                       CHECK (calendar IN ('MOEX_STOCK', 'MOEX_FX', 'MOEX_FORTS')),
    day                date NOT NULL,
    source             text NOT NULL,
    trade_session_date date,
    known_at           timestamptz NOT NULL,
    note               text,
    capture_id         bigint REFERENCES ops.raw_capture (id),
    run_id             bigint,
    PRIMARY KEY (calendar, day, source)
);
CREATE INDEX IF NOT EXISTS weekend_session_capture_idx ON ref.weekend_session (capture_id);
COMMENT ON TABLE ref.weekend_session IS
    'Days on which the engine of a calendar trades although they are not settlement business days '
    '(MOEX ISS engines/{engine}/calendar.json rows with is_work_day=1 and reason W: weekend / '
    'holiday sessions whose trades belong to trade_session_date). Trading day of an engine = '
    'business day (ref.calendar_effective) or a day listed here (mdhub.core.bizday '
    'CalendarPredicate.is_trading_day). ref.calendar_day keeps these days non-business.';
CALL ops.enable_history('ref.weekend_session', ARRAY['calendar', 'day', 'source'],
                        ARRAY['known_at', 'note']);

-- ------------------------------------------- N9 capture_id indexes (retention, FK actions)
-- Every table referencing ops.raw_capture without an index led by capture_id (md.zcyc_reject
-- has its primary key on capture_id). Needed by the retention NOT EXISTS probes and by the
-- ON DELETE SET NULL actions when intraday captures are deleted.
CREATE INDEX IF NOT EXISTS bond_eod_capture_idx ON md.bond_eod (capture_id);
CREATE INDEX IF NOT EXISTS bond_eod_yield_capture_idx ON md.bond_eod_yield (capture_id);
CREATE INDEX IF NOT EXISTS bond_quote_intraday_capture_idx ON md.bond_quote_intraday (capture_id);
CREATE INDEX IF NOT EXISTS bond_quote_last_capture_idx ON md.bond_quote_last (capture_id);
CREATE INDEX IF NOT EXISTS index_eod_capture_idx ON md.index_eod (capture_id);
CREATE INDEX IF NOT EXISTS zcyc_snapshot_capture_idx ON md.zcyc_snapshot (capture_id);
CREATE INDEX IF NOT EXISTS fx_eod_capture_idx ON md.fx_eod (capture_id);
CREATE INDEX IF NOT EXISTS fx_intraday_capture_idx ON md.fx_intraday (capture_id);
CREATE INDEX IF NOT EXISTS fx_indicative_capture_idx ON md.fx_indicative (capture_id);
CREATE INDEX IF NOT EXISTS fx_fixing_capture_idx ON md.fx_fixing (capture_id);
CREATE INDEX IF NOT EXISTS futures_eod_capture_idx ON md.futures_eod (capture_id);
CREATE INDEX IF NOT EXISTS futures_intraday_capture_idx ON md.futures_intraday (capture_id);
CREATE INDEX IF NOT EXISTS algopack_fo_tradestats_capture_idx
    ON md.algopack_fo_tradestats (capture_id);
CREATE INDEX IF NOT EXISTS algopack_fo_obstats_capture_idx ON md.algopack_fo_obstats (capture_id);
CREATE INDEX IF NOT EXISTS algopack_futoi_capture_idx ON md.algopack_futoi (capture_id);
CREATE INDEX IF NOT EXISTS fixing_capture_idx ON md.fixing (capture_id);
CREATE INDEX IF NOT EXISTS fx_official_capture_idx ON md.fx_official (capture_id);
CREATE INDEX IF NOT EXISTS cbr_liquidity_capture_idx ON md.cbr_liquidity (capture_id);
CREATE INDEX IF NOT EXISTS ecb_fx_capture_idx ON md.ecb_fx (capture_id);
CREATE INDEX IF NOT EXISTS curve_snapshot_capture_idx ON md.curve_snapshot (capture_id);
CREATE INDEX IF NOT EXISTS fx_instrument_capture_idx ON ref.fx_instrument (capture_id);
CREATE INDEX IF NOT EXISTS futures_contract_capture_idx ON ref.futures_contract (capture_id);
CREATE INDEX IF NOT EXISTS cbr_meeting_capture_idx ON ref.cbr_meeting (capture_id);
CREATE INDEX IF NOT EXISTS calendar_day_capture_idx ON ref.calendar_day (capture_id);
CREATE INDEX IF NOT EXISTS issuer_capture_idx ON ref.issuer (capture_id);
CREATE INDEX IF NOT EXISTS bond_capture_idx ON ref.bond (capture_id);
CREATE INDEX IF NOT EXISTS bond_board_capture_idx ON ref.bond_board (capture_id);
CREATE INDEX IF NOT EXISTS bond_coupon_capture_idx ON ref.bond_coupon (capture_id);
CREATE INDEX IF NOT EXISTS bond_amortization_capture_idx ON ref.bond_amortization (capture_id);
CREATE INDEX IF NOT EXISTS bond_offer_capture_idx ON ref.bond_offer (capture_id);
CREATE INDEX IF NOT EXISTS bond_rate_formula_capture_idx ON ref.bond_rate_formula (capture_id);
CREATE INDEX IF NOT EXISTS bond_rating_capture_idx ON ref.bond_rating (capture_id);
CREATE INDEX IF NOT EXISTS static_snapshot_capture_idx ON ref.static_snapshot (capture_id);
CREATE INDEX IF NOT EXISTS static_conflict_capture_idx ON ref.static_conflict (capture_id);
