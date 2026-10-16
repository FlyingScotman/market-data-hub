-- 0422_target_calendar: TARGET business-day calendar (euro area; dataset ecb_fx).
-- One source, rule_target, for past and future days (2002 .. end of next year): weekends and
-- the TARGET closing days computed by mdhub.loaders.calendar.target_closing_days (Jan 1, Good
-- Friday, Easter Monday, May 1, Dec 25, Dec 26). ECB rate dates (md.ecb_fx) only cross-check
-- the rule in the calendar refresh. Only altered constraints / replaced function and comments:
-- earlier migrations stay untouched. Safe on a populated database (existing rows all
-- satisfy the widened CHECK; no view depends on the constraint).

-- ------------------------------------------------- ref.calendar_day.calendar: allow TARGET
-- The original CHECK (0250) is unnamed; find it by its definition.
DO $$
DECLARE
    c text;
BEGIN
    FOR c IN
        SELECT con.conname FROM pg_constraint con
        WHERE con.conrelid = 'ref.calendar_day'::regclass AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%RU_PRODUCTION%'
    LOOP
        EXECUTE format('ALTER TABLE ref.calendar_day DROP CONSTRAINT %I', c);
    END LOOP;
END;
$$;
ALTER TABLE ref.calendar_day ADD CONSTRAINT calendar_day_calendar_chk
    CHECK (calendar IN ('MOEX_STOCK', 'MOEX_FX', 'MOEX_FORTS', 'RU_PRODUCTION', 'TARGET'));

-- ------------------------------------------------------------ source rank: rule_* sources
-- Computed holiday rules (rule_target) rank below the curated file (a curated entry can
-- override the rule, e.g. an extraordinary closure) and above unverified curated guesses.
-- Mirrors mdhub.loaders.calendar.SOURCE_PRIORITY. Same signature: dependent views stay valid.
CREATE OR REPLACE FUNCTION ref.calendar_source_rank(source text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN source LIKE 'observed\_%' THEN 1
        WHEN source = 'moex_iss_calendar' THEN 2
        WHEN source = 'curated' THEN 3
        WHEN source LIKE 'rule\_%' THEN 4
        WHEN source = 'curated_unverified' THEN 5
        ELSE 9
    END
$$;
COMMENT ON FUNCTION ref.calendar_source_rank(text) IS
    'Precedence of ref.calendar_day sources (1 = highest): observed_* > moex_iss_calendar > '
    'curated > rule_* > curated_unverified > other';

COMMENT ON VIEW ref.calendar_effective IS
    'Business-day flag per (calendar, day) from the highest-priority source '
    '(ref.calendar_source_rank: observed > MOEX ISS calendar rows > curated > rule > '
    'curated_unverified; known_at breaks ties). Days without any row: weekday rule in '
    'mdhub.core.bizday';
COMMENT ON TABLE ref.calendar_day IS
    'Business days per calendar (MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION, TARGET) and '
    'source; consumers take, per (calendar, day), the row of the highest-priority source '
    '(ref.calendar_source_rank; ref.calendar_effective, mdhub.core.bizday.DbCalendar). Sources: '
    'observed_* (past days from MOEX trading / CBR RUONIA observations, from 2019), '
    'moex_iss_calendar (only the days the official MOEX ISS calendar lists), curated / '
    'curated_unverified (versioned file mdhub/data/calendar_curated.json), rule_target (TARGET, '
    'all days from 2002: weekends and the computed closing days Jan 1, Good Friday, Easter '
    'Monday, May 1, Dec 25, Dec 26).';
