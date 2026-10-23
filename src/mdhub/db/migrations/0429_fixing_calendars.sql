-- 0429_fixing_calendars: the calendars of RUB swap and fixing conventions (contract 1.6, TDM
-- brief H17, market-data-hub#38): MOEX_SPFI (trading days of the MOEX standardised derivatives
-- market, the "SPFI" calendar of the MOEX swap specifications), CBR_RUONIA (days for which the
-- Bank of Russia calculates and publishes RUONIA) and MOEX_RUSFAR (calculation days of the RUB
-- overnight RUSFAR). All three are derived by the calendar refresh (mdhub.loaders.calendar) from
-- the effective MOEX_FORTS / RU_PRODUCTION / MOEX_STOCK days plus documented rules (sources
-- rule_moex_spfi, rule_cbr_ruonia, rule_moex_rusfar) and curated exceptions (source curated,
-- mdhub/data/calendar_derived.json). A day whose flag rests on a provisional input (an
-- unverified curated year, an undecided year-end) is written by the source with the suffix
-- _unverified and ranks with curated_unverified. Only the CHECK, the rank function and comments
-- change (as in 0422, 0428): safe on a populated database.

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
    CHECK (calendar IN ('MOEX_STOCK', 'MOEX_FX', 'MOEX_FORTS', 'RU_PRODUCTION', 'TARGET',
                        'CN_INTERBANK', 'US_FED', 'CH_ZURICH', 'MOEX_SPFI', 'CBR_RUONIA',
                        'MOEX_RUSFAR'));

CREATE OR REPLACE FUNCTION ref.calendar_source_rank(source text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN source LIKE 'observed\_%' THEN 1
        WHEN source = 'moex_iss_calendar' THEN 2
        WHEN source = 'curated' THEN 3
        WHEN source LIKE '%\_unverified' THEN 5
        WHEN source LIKE 'rule\_%' THEN 4
        ELSE 9
    END
$$;
COMMENT ON FUNCTION ref.calendar_source_rank(text) IS
    'Precedence of ref.calendar_day sources (1 = highest): observed_* > moex_iss_calendar > '
    'curated > rule_* > *_unverified (curated_unverified, rule_*_unverified: provisional) > other';

COMMENT ON TABLE ref.calendar_day IS
    'Business days per calendar (MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION, TARGET, '
    'CN_INTERBANK, US_FED, CH_ZURICH, MOEX_SPFI, CBR_RUONIA, MOEX_RUSFAR) and source; consumers '
    'take, per (calendar, day), the row of the highest-priority source '
    '(ref.calendar_source_rank; ref.calendar_effective, mdhub.core.bizday.DbCalendar). Sources: '
    'observed_* (past days from MOEX trading, from 2019), moex_iss_calendar (only the days the '
    'official MOEX ISS calendar lists), curated / curated_unverified (versioned files '
    'mdhub/data/calendar_curated.json and, for the derived calendars, calendar_derived.json), '
    'rule_target (TARGET), rule_us_fed (US_FED), rule_ch_zurich (CH_ZURICH): all days from 2002 '
    'by the computed holiday rule plus weekends; rule_moex_spfi, rule_cbr_ruonia, '
    'rule_moex_rusfar (and their _unverified variants): derived from the effective days of '
    'MOEX_FORTS / RU_PRODUCTION / MOEX_STOCK by the documented rule (contract 1.6).';
