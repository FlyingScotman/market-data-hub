-- Issuer reference (WEB-13 / TDM brief R9 HS1, HS3, HS4).
--
--   ref.issuer_card   one MOEX ISS /iss/emitters/{id}.json card per issuer (HS1): TITLE,
--                     SHORT_TITLE, INN, OGRN, OKPO, OKSM and the ISO 3166-1 alpha-2 country
--                     derived from OKSM. last_seen = last fetch (refresh cache, meta column).
--   ref.issuer        new columns:
--                       oksm            numeric country code of the card (HS1, raw)
--                       sector_source   okved | curated | group | rule: where sector came from (HS3)
--                       okved, okved_version  main OKVED2 activity, not loaded yet (HS3b; FNS
--                                       registers are not reachable from the hub)
--                       group_id, group_name, group_relation, is_state_owned, group_evidence
--                                       curated issuer group (HS4, mdhub/data/issuer_groups_curated.json)
--                     short_name, ogrn, country are filled from ref.issuer_card; sector from the
--                     versioned rules of mdhub/data/sector_map.json (job issuers_static).
-- Both tables keep their changes in ops.row_history (as-known-at readers: 0520).

CREATE TABLE ref.issuer_card (
    issuer_id     bigint PRIMARY KEY,           -- MOEX EMITTER_ID
    title         text,
    short_title   text,
    inn           text,
    ogrn          text,
    okpo          text,
    oksm          integer,
    country       text,
    found         boolean NOT NULL DEFAULT true,
    source        text NOT NULL DEFAULT 'moex_iss',
    run_id        bigint,
    capture_id    bigint REFERENCES ops.raw_capture (id),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz
);
CREATE INDEX issuer_card_capture_idx ON ref.issuer_card (capture_id);
COMMENT ON TABLE ref.issuer_card IS 'MOEX ISS emitter card per issuer (/iss/emitters/{id}.json), refreshed weekly by job issuers_static';
COMMENT ON COLUMN ref.issuer_card.country IS 'ISO 3166-1 alpha-2 from OKSM (mdhub/data/oksm_iso3166.json); NULL when OKSM is empty or unknown';
COMMENT ON COLUMN ref.issuer_card.found IS 'false: ISS answered with an empty emitter block (no card for this id)';
COMMENT ON COLUMN ref.issuer_card.last_seen IS 'Time of the last fetch (meta: refresh cache, no history)';
CALL ops.enable_history('ref.issuer_card', ARRAY['issuer_id']);

ALTER TABLE ref.issuer
    ADD COLUMN oksm            integer,
    ADD COLUMN sector_source   text,
    ADD COLUMN okved           text,
    ADD COLUMN okved_version   text,
    ADD COLUMN group_id        text,
    ADD COLUMN group_name      text,
    ADD COLUMN group_relation  text,
    ADD COLUMN is_state_owned  boolean,
    ADD COLUMN group_evidence  jsonb,
    ADD CONSTRAINT issuer_sector_source_chk
        CHECK (sector_source IN ('okved', 'curated', 'group', 'rule')),
    ADD CONSTRAINT issuer_group_relation_chk
        CHECK (group_relation IN ('parent', 'subsidiary', 'spv', 'securitization_spv', 'affiliate')),
    ADD CONSTRAINT issuer_group_complete_chk
        CHECK ((group_id IS NULL) = (group_name IS NULL) AND (group_id IS NULL) = (group_relation IS NULL));
COMMENT ON COLUMN ref.issuer.oksm IS 'OKSM (ISO 3166-1 numeric) country code of the MOEX emitter card';
COMMENT ON COLUMN ref.issuer.sector IS 'Desk sector code (mdhub/data/sector_map.json taxonomy)';
COMMENT ON COLUMN ref.issuer.sector_source IS 'okved (OKVED mapping) | curated (per-INN entry) | group (curated issuer group) | rule (classification/name rule)';
COMMENT ON COLUMN ref.issuer.okved IS 'Main OKVED2 activity code (not loaded yet: FNS registers not reachable)';
COMMENT ON COLUMN ref.issuer.group_id IS 'Curated issuer group (mdhub/data/issuer_groups_curated.json)';
COMMENT ON COLUMN ref.issuer.group_evidence IS 'Evidence of the curated group row (file version, references)';
