-- market-data-hub#165 follow-up: the issuer-data source's own statement whether a bond's coupon
-- is floating, kept as a three-valued fact. Cbonds publishes `floating_rate` per emission; the
-- hub used only "true" (to write a rate formula) and dropped "false" and "no data". The
-- classification of an unlabelled bond whose rate stepped up long ago
-- (mdhub.sources.moex_static.schedule_tail_constant_fixed) needs the explicit "not floating".
--
-- floating_rate: true / false as the source states, NULL = the source states nothing (or has
-- not been read). floating_rate_source: the source ('cbonds'; NULL with floating_rate NULL).
-- floating_rate_updated_on: the source's own update date of that emission (Cbonds
-- updating_date), not the fetch date (a daily fetch date would write a row version per bond per
-- day); the fetch is visible in updated_at / ops.job_run. Written by job cbonds_enrich next to
-- day_count (day_count_source); history and the as-known-at reader of ref.bond cover them.
-- Number: ref column, so the ref range (DEVELOPING.md); the runner applies pending files by
-- name, so it also runs on a database where 0500 and later are already applied (the same as
-- 0122), and it touches nothing those migrations created: ref.bond_known_at reads the table's
-- current columns, history images written before this column lack it and read as NULL.
ALTER TABLE ref.bond
    ADD COLUMN floating_rate              boolean,
    ADD COLUMN floating_rate_source       text,
    ADD COLUMN floating_rate_updated_on   date,
    ADD COLUMN floating_rate_base_present boolean;
ALTER TABLE ref.bond ADD CONSTRAINT bond_floating_rate_chk CHECK (
    (floating_rate IS NULL AND floating_rate_source IS NULL
        AND floating_rate_base_present IS NULL)
    OR (floating_rate IS NOT NULL AND floating_rate_source IS NOT NULL
        AND floating_rate_source IN ('cbonds')));
COMMENT ON COLUMN ref.bond.floating_rate IS
    'Whether the coupon is floating according to floating_rate_source (Cbonds floating_rate): '
    'true / false as stated, NULL = no statement (never read, or the source leaves it empty)';
COMMENT ON COLUMN ref.bond.floating_rate_source IS
    'cbonds; NULL if and only if floating_rate is NULL';
COMMENT ON COLUMN ref.bond.floating_rate_updated_on IS
    'Date the source last updated the emission (Cbonds updating_date); not the fetch date';
COMMENT ON COLUMN ref.bond.floating_rate_base_present IS
    'Cbonds states a base rate (reference_rate_name_rus) or a margin for the emission, read with '
    'floating_rate; a bond with floating_rate = false and a base rate stated is contradictory '
    'and is not classified fixed by its schedule';
