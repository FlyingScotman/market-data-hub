-- WEB-13 / TDM brief R9 HS6: coupon_type_basis 'amortizer_constant_rate' (after 0421, which
-- re-created the CHECK): an active amortizer (MOEX BOND_TYPE amortizer label) without
-- COUPON_DETAILS, COUPON_BENCHMARK or a rate formula whose fully valued schedule carries one
-- positive rate is classified fixed (mdhub.sources.moex_static.amortizer_constant_fixed).
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
    'amortizer_constant_rate', 'none'));
COMMENT ON COLUMN ref.bond.coupon_type_basis IS
    'moex_label | benchmark | ofz_series | schedule (matured bond: fixed from its paid schedule; '
    'active unlabelled bond with a fully valued schedule: coupon_type unknown) | schedule_constant '
    '(active unlabelled bond classified fixed: no benchmark / COUPON_DETAILS and no rate formula '
    'from any source, >= 2 paid coupons, one constant rate (value_prc) over all paid and future '
    'coupons, and the paid coupon periods span at least one CBR key-rate change, so a floater on an '
    'unchanged key rate cannot pass) | amortizer_constant_rate (active amortizer by MOEX BOND_TYPE, '
    'no COUPON_DETAILS / COUPON_BENCHMARK / rate formula, fully valued schedule with one positive '
    'rate; weaker evidence than schedule_constant: no key-rate change needed) | none';
