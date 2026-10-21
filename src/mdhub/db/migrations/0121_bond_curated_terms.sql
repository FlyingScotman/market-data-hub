-- Issue terms curated from issue documents, with evidence (WEB-17; TDM brief R9 HS8 floater
-- conventions, HS9 call schedules of perpetuals, HS10 dated offers; review P1-6 replacement
-- bonds by document, P1-9). Source file: mdhub/data/bond_terms_curated.json, job
-- bond_terms_curated. Every row carries the document it was read from (URL, title, page,
-- extracted text): a value without evidence is rejected by the loader and by the CHECK below.
--
-- The job also writes the terms where the existing readers expect them:
--   floater_formula -> ref.bond_rate_formula (source 'curated', status 'curated'),
--   call / offer    -> ref.bond_offer (source 'curated'),
-- so ref.bond_rate_formula_resolved prefers the documented conventions over MOEX/Cbonds
-- inferences. replacement_bond is read by the static snapshot builder (basis 'document').

CREATE TABLE ref.bond_curated_term (
    secid         text NOT NULL,
    term          text NOT NULL CHECK (term IN (
                      'floater_formula', 'call', 'offer', 'replacement_bond')),
    term_key      text NOT NULL DEFAULT '',   -- call / offer: ISO date; others ''
    value         jsonb NOT NULL,
    evidence      jsonb NOT NULL CHECK (
                      evidence ? 'document_url' AND evidence ? 'document_title'
                      AND evidence ? 'extracted_text'),
    file_version  text NOT NULL,
    is_deleted    boolean NOT NULL DEFAULT false,
    run_id        bigint,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_run bigint,
    PRIMARY KEY (secid, term, term_key),
    CONSTRAINT bond_curated_term_secid_fk FOREIGN KEY (secid) REFERENCES ref.bond (secid)
        DEFERRABLE INITIALLY DEFERRED
);
COMMENT ON TABLE ref.bond_curated_term IS
    'Issue terms read from issue documents (decision on issue, prospectus, NSD/e-disclosure '
    'filings) with evidence per row; mdhub/data/bond_terms_curated.json, job bond_terms_curated. '
    'Removed entries are soft-deleted (is_deleted), versioned in ops.row_history';
CALL ops.enable_history('ref.bond_curated_term', ARRAY['secid', 'term', 'term_key']);

