-- Manual correction of the future redemption schedule (market-data-hub#165). MOEX ISS publishes
-- the principal schedule rounded (value in steps of 0.01, valueprc in whole percent), so a bond
-- with a low face (e.g. face 0.035, seven redemptions of 0.005 that read 0.01) has a schedule
-- that does not add up to its face. A reviewed, versioned file lists the true amounts; the job
-- bond_terms_curated writes them here. Source file: mdhub/data/bond_amortization_overrides.json.
--
-- Rows are NOT merged into ref.bond_amortization (the MOEX loader owns that table and would
-- overwrite them). The snapshot builder and the TDM export read both and apply an override only
-- while it agrees with what MOEX publishes (mdhub.snapshot.redemptions.plan_schedule).

CREATE TABLE ref.bond_amortization_override (
    secid         text NOT NULL,
    amort_date    date NOT NULL,
    value         numeric NOT NULL CHECK (value > 0),   -- per bond, in face_unit
    face_unit     text NOT NULL CHECK (face_unit <> ''),
    source_note   text NOT NULL CHECK (btrim(source_note) <> ''),
    confirmed_by  text NOT NULL CHECK (btrim(confirmed_by) <> ''),
    confirmed_on  date NOT NULL,
    file_version  text NOT NULL,
    is_deleted    boolean NOT NULL DEFAULT false,
    run_id        bigint,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_run bigint,
    PRIMARY KEY (secid, amort_date),
    CONSTRAINT bond_amortization_override_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE ref.bond_amortization_override IS
    'Redemption amounts of a bond confirmed by a person (source_note, confirmed_by, confirmed_on), '
    'replacing the MOEX rows of that bond from its first date on while they agree with MOEX; '
    'mdhub/data/bond_amortization_overrides.json, job bond_terms_curated. Removed entries are '
    'soft-deleted (is_deleted), versioned in ops.row_history';
CALL ops.enable_history('ref.bond_amortization_override', ARRAY['secid', 'amort_date']);
