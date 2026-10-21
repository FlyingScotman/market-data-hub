-- Credit ratings from the public disclosure of the Russian rating agencies (WEB-18, review
-- P1-1; TDM brief R9 HS5): job ratings_agencies.
--
--   ref.agency_rating   current state per rated object as the agency publishes it (verbatim
--                       rating / outlook / watch, agency identifiers ISIN / INN / OGRN, press
--                       release); one row per (agency, object, rating type, scale). Revisions
--                       are versioned in ops.row_history (as-known-at reader below).
--   ref.rating_action   history of rating actions (one row per published action); insert-only
--                       by natural key, revisions versioned.
--   ref.agency_page     crawl state of the agencies without a list export (RA Expert: press
--                       releases discovered from the sitemap, then the company / region / bond
--                       pages they link to); no data, only bookkeeping.
--   ref.issuer_rating   issuer-scope ratings mapped to MOEX issuers (by INN; regions by the
--                       ISINs of their rated bonds), as ref.bond_rating is for issues.
--   ref.bond_rating     + watch, action, grade, rating_type, evidence (agency rows; Cbonds rows
--                       keep NULLs).

INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('nkr', 'НКР (ООО «Национальные Кредитные Рейтинги»)', 'https://www.ratings.ru',
     'Public rating lists (issues.php / issuers.php exports). Reproduction and distribution only '
     'with the agency''s prior written consent: internal desk use, not for redistribution'),
    ('nra', 'НРА (ООО «Национальное Рейтинговое Агентство»)', 'https://www.ra-national.ru',
     'Public rating action export (ratings page, "Выгрузить в Excel"). Agency terms apply: '
     'internal desk use, not for redistribution'),
    ('raexpert', 'Эксперт РА (АО «Эксперт РА»)', 'https://raexpert.ru',
     'Public press releases and rating pages. The site forbids distribution without the '
     'agency''s consent; bulk export / API is a paid subscription: internal desk use only'),
    ('acra', 'АКРА (АО)', 'https://www.acra-ratings.ru',
     'Public rating disclosure. Not loaded yet: the site''s TLS chain omits its intermediate '
     'certificate (GlobalSign GCC R46 OV TLS CA 2025), see ROADMAP WEB-18')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------- ref.agency_rating
CREATE TABLE ref.agency_rating (
    agency          text NOT NULL REFERENCES ops.source (code),
    object_kind     text NOT NULL CHECK (object_kind IN ('issuer', 'issue')),
    object_key      text NOT NULL,   -- issue: ISIN; issuer: INN, else agency id (agency:<id>)
    rating_type     text NOT NULL,   -- agency's rating type / list name, verbatim
    scale           text NOT NULL,   -- national scale label (e.g. 'national_ru')
    agency_object_id text,           -- agency's own id / page of the object
    object_name     text,
    isin            text,
    inn             text,
    ogrn            text,
    rating          text,            -- verbatim; NULL when withdrawn ('—', 'отозван')
    grade           text,            -- normalized letter grade (mdhub.sources.ratings_common)
    outlook         text,            -- verbatim
    watch           text,            -- verbatim review / watch status; NULL when none
    action          text,            -- normalized: assigned | affirmed | upgraded | downgraded |
                                     -- withdrawn | changed | outlook_changed | unknown
    action_raw      text,            -- verbatim action / status as published
    rating_date     date,            -- date of the latest action (press release)
    press_release   text,            -- URL of the press release of the latest action
    industry        text,            -- agency's sector / industry of the rated entity, verbatim
    is_withdrawn    boolean NOT NULL DEFAULT false,
    source_url      text,            -- list export / page the row was parsed from (no secrets)
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- parser context (e.g. region page)
    run_id          bigint,
    capture_id      bigint REFERENCES ops.raw_capture (id),
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_run   bigint,
    last_seen       timestamptz,
    PRIMARY KEY (agency, object_kind, object_key, rating_type, scale)
);
CREATE INDEX agency_rating_isin_idx ON ref.agency_rating (isin) WHERE isin IS NOT NULL;
CREATE INDEX agency_rating_inn_idx ON ref.agency_rating (inn) WHERE inn IS NOT NULL;
CREATE INDEX agency_rating_capture_idx ON ref.agency_rating (capture_id);
COMMENT ON TABLE ref.agency_rating IS
    'Credit ratings as the agencies publish them (NKR, NRA, RA Expert; job ratings_agencies). '
    'Latest state per object; withdrawn ratings stay with is_withdrawn, rating NULL. '
    'Versioned in ops.row_history';

-- ----------------------------------------------------------------------- ref.rating_action
CREATE TABLE ref.rating_action (
    agency          text NOT NULL REFERENCES ops.source (code),
    object_kind     text NOT NULL CHECK (object_kind IN ('issuer', 'issue')),
    object_key      text NOT NULL,
    rating_type     text NOT NULL,
    scale           text NOT NULL,
    action_date     date NOT NULL,
    action_ref      text NOT NULL,   -- press release URL (or agency action id); '' if none
    rating          text,
    grade           text,
    outlook         text,
    watch           text,
    action          text,
    action_raw      text,
    title           text,            -- press release title, verbatim (when published)
    run_id          bigint,
    capture_id      bigint REFERENCES ops.raw_capture (id),
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_run   bigint,
    PRIMARY KEY (agency, object_kind, object_key, rating_type, scale, action_date, action_ref)
);
CREATE INDEX rating_action_capture_idx ON ref.rating_action (capture_id);
COMMENT ON TABLE ref.rating_action IS
    'Rating actions (history) per agency object: NRA export rows, RA Expert page history rows, '
    'NKR list states as observed. Never deleted; revisions versioned in ops.row_history';

-- ------------------------------------------------------------------------- ref.agency_page
CREATE TABLE ref.agency_page (
    agency          text NOT NULL REFERENCES ops.source (code),
    url             text NOT NULL,   -- canonical https URL, no query string
    kind            text NOT NULL CHECK (kind IN ('release', 'company', 'region', 'bond')),
    page_date       date,            -- release date from the URL, when it has one
    discovered_at   timestamptz NOT NULL DEFAULT now(),
    discovered_from text,            -- sitemap | release URL that linked it
    linked_at       timestamptz,     -- last time a newly fetched release linked it
    fetched_at      timestamptz,
    http_status     integer,
    content_sha256  text,
    error           text,
    capture_id      bigint REFERENCES ops.raw_capture (id),
    PRIMARY KEY (agency, url)
);
CREATE INDEX agency_page_due_idx ON ref.agency_page (agency, kind, fetched_at);
CREATE INDEX agency_page_capture_idx ON ref.agency_page (capture_id);
COMMENT ON TABLE ref.agency_page IS
    'Crawl bookkeeping of rating pages (RA Expert): what was discovered, fetched, when and with '
    'which result. Rating values live in ref.agency_rating / ref.rating_action';

-- ----------------------------------------------------------------------- ref.issuer_rating
CREATE TABLE ref.issuer_rating (
    issuer_id      bigint NOT NULL,
    agency         text NOT NULL,   -- agency display name (НКР, НРА, Эксперт РА)
    scale          text NOT NULL DEFAULT '',
    source         text NOT NULL,   -- hub source code (nkr, nra, raexpert)
    rating_type    text NOT NULL DEFAULT '',
    rating         text,
    grade          text,
    outlook        text,
    watch          text,
    action         text,
    rating_date    date,
    evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- mapping basis, agency keys, release
    run_id         bigint,
    capture_id     bigint REFERENCES ops.raw_capture (id),
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_run  bigint,
    PRIMARY KEY (issuer_id, agency, scale, source, rating_type),
    CONSTRAINT issuer_rating_issuer_fk FOREIGN KEY (issuer_id) REFERENCES ref.issuer (issuer_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX issuer_rating_capture_idx ON ref.issuer_rating (capture_id);
COMMENT ON TABLE ref.issuer_rating IS
    'Issuer ratings mapped to MOEX issuers (ref.issuer.issuer_id): by INN, regions by the ISINs '
    'of their rated bonds (evidence.mapping_basis). Withdrawn ratings stay with action withdrawn';

-- ------------------------------------------------------------------------ ref.bond_rating
ALTER TABLE ref.bond_rating
    ADD COLUMN watch       text,
    ADD COLUMN action      text,
    ADD COLUMN grade       text,
    ADD COLUMN rating_type text,
    ADD COLUMN evidence    jsonb;
COMMENT ON TABLE ref.bond_rating IS
    'Latest issue rating per agency/scale and source: Cbonds get_rating_emission_maxdate '
    '(source cbonds) and the agencies'' public disclosure mapped by ISIN (sources nkr, nra, '
    'raexpert; job ratings_agencies). Changes in ops.row_history';

-- ------------------------------------------------------------------------------ history
-- (as-known-at readers: 0540 / 0541, after ops.create_known_at_function of 0500; job sources: 0612)
CALL ops.enable_history('ref.agency_rating',
                        ARRAY['agency', 'object_kind', 'object_key', 'rating_type', 'scale']);
CALL ops.enable_history('ref.rating_action',
                        ARRAY['agency', 'object_kind', 'object_key', 'rating_type', 'scale',
                              'action_date', 'action_ref']);
CALL ops.enable_history('ref.issuer_rating',
                        ARRAY['issuer_id', 'agency', 'scale', 'source', 'rating_type']);

-- ------------------------------------------------------- ref.issuer.sector_source 'agency'
-- Sector from the industry a rating agency publishes for the rated issuer (NKR / NRA / RA
-- Expert, mapped by mdhub/data/sector_map.json agency_industries), after okved and the bond
-- class rules, before the name rules (mdhub.loaders._issuer_rules.assign_sector).
ALTER TABLE ref.issuer DROP CONSTRAINT issuer_sector_source_chk;
ALTER TABLE ref.issuer ADD CONSTRAINT issuer_sector_source_chk
    CHECK (sector_source IN ('okved', 'curated', 'group', 'rule', 'agency'));
COMMENT ON COLUMN ref.issuer.sector_source IS
    'okved (OKVED mapping) | curated (per-INN entry) | group (curated issuer group) | rule '
    '(classification/name rule) | agency (industry published by a rating agency)';
