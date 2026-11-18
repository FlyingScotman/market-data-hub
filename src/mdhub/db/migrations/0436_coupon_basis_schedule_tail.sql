-- market-data-hub#165 follow-up: coupon_type_basis 'schedule_tail_constant' (after 0426, which
-- re-created the CHECK): an active unlabelled bond whose rate stepped up years ago (e.g. 2.25 ->
-- 7.5 % until 2007) has one rate only over the tail of its schedule; the tail (the paid coupons
-- at the rate of the last coupon plus every future one) is judged like a whole schedule is for
-- schedule_constant, but only for an active bond that Cbonds explicitly calls not floating
-- (0123; mdhub.sources.moex_static.schedule_tail_constant_fixed).
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
    'moex_label', 'benchmark', 'ofz_series', 'schedule', 'schedule_constant',
    'schedule_tail_constant', 'amortizer_constant_rate', 'none'));
COMMENT ON COLUMN ref.bond.coupon_type_basis IS
    'moex_label | benchmark | ofz_series | schedule (matured bond: fixed from its paid schedule; '
    'active unlabelled bond with a fully valued schedule: coupon_type unknown) | schedule_constant '
    '(active unlabelled bond classified fixed: no benchmark / COUPON_DETAILS and no rate formula '
    'from any source, >= 2 paid coupons, one constant rate (value_prc) over all paid and future '
    'coupons, and the paid coupon periods span at least one CBR key-rate change, so a floater on an '
    'unchanged key rate cannot pass) | schedule_tail_constant (the same test on the tail of the '
    'schedule - the coupons at the rate of the last coupon, including every future one - for an '
    'active bond whose early coupons paid other rates and that Cbonds states not floating '
    '(ref.bond.floating_rate = false); >= 2 paid coupons in the tail and a CBR key-rate change '
    'inside it) | amortizer_constant_rate (active amortizer by MOEX BOND_TYPE, '
    'no COUPON_DETAILS / COUPON_BENCHMARK / rate formula, fully valued schedule with one positive '
    'rate; weaker evidence than schedule_constant: no key-rate change needed) | none';
