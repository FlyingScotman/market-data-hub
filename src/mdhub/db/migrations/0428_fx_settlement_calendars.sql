-- 0428_fx_settlement_calendars: foreign settlement calendars for FX value dates (contract 1.4,
-- TDM brief H13, package H4): CN_INTERBANK (China interbank market, State Council holiday
-- notices with the adjusted working weekends; source curated, decreed years only), US_FED
-- (Federal Reserve Banks holidays, source rule_us_fed) and CH_ZURICH (Zurich bank holidays,
-- source rule_ch_zurich), written by the calendar refresh (mdhub.loaders.calendar). Only the
-- CHECK and the comment change (as in 0422): safe on a populated database.

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
                        'CN_INTERBANK', 'US_FED', 'CH_ZURICH'));

COMMENT ON TABLE ref.calendar_day IS
    'Business days per calendar (MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION, TARGET, '
    'CN_INTERBANK, US_FED, CH_ZURICH) and source; consumers take, per (calendar, day), the row '
    'of the highest-priority source (ref.calendar_source_rank; ref.calendar_effective, '
    'mdhub.core.bizday.DbCalendar). Sources: observed_* (past days from MOEX trading / CBR RUONIA '
    'observations, from 2019), moex_iss_calendar (only the days the official MOEX ISS calendar '
    'lists), curated / curated_unverified (versioned file mdhub/data/calendar_curated.json; '
    'CN_INTERBANK: the State Council notice of each decreed year), rule_target (TARGET), '
    'rule_us_fed (US_FED), rule_ch_zurich (CH_ZURICH): all days from 2002 by the computed '
    'holiday rule plus weekends.';
