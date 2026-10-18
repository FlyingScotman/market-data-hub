-- 0600_admin_api_token: schema admin (secrets and identities, ADMIN_DESIGN §3.1) with the named
-- consumer tokens of the read API (§3.3). Migration range 0600-0699 = admin console and
-- observability (ADMIN_DESIGN §3).
--
-- The admin schema is NOT readable by the read-only consumer role. deploy/sql/grant_readonly.sql
-- skips it, but the owner's global default privileges (deploy/sql/readonly_defaults.sql:
-- USAGE ON SCHEMAS, SELECT ON TABLES/SEQUENCES) also apply to objects created here, and
-- PostgreSQL cannot subtract a schema from global defaults. So every migration that creates
-- objects in admin ends with CALL admin.restrict_access(), which revokes everything in admin
-- from PUBLIC and from every role except the schema owner (tests/test_deploy.py checks it).

CREATE SCHEMA IF NOT EXISTS admin;
COMMENT ON SCHEMA admin IS 'Admin console secrets and identities (consumer tokens; users, sessions later). Never granted to read-only roles';

-- ------------------------------------------------------------------- admin.restrict_access
CREATE OR REPLACE PROCEDURE admin.restrict_access()
LANGUAGE plpgsql AS $$
DECLARE
    owner_oid oid;
    grantee   text;
BEGIN
    SELECT nspowner INTO owner_oid FROM pg_namespace WHERE nspname = 'admin';
    REVOKE ALL ON SCHEMA admin FROM PUBLIC;
    REVOKE ALL ON ALL TABLES IN SCHEMA admin FROM PUBLIC;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA admin FROM PUBLIC;
    REVOKE ALL ON ALL ROUTINES IN SCHEMA admin FROM PUBLIC;
    FOR grantee IN
        SELECT DISTINCT pg_get_userbyid(a.grantee)
        FROM (
            SELECT (aclexplode(n.nspacl)).grantee FROM pg_namespace n WHERE n.nspname = 'admin'
            UNION ALL
            SELECT (aclexplode(c.relacl)).grantee
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'admin'
            UNION ALL
            SELECT (aclexplode(p.proacl)).grantee
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'admin'
        ) a
        WHERE a.grantee <> 0 AND a.grantee <> owner_oid
    LOOP
        EXECUTE format('REVOKE ALL ON SCHEMA admin FROM %I', grantee);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA admin FROM %I', grantee);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA admin FROM %I', grantee);
        EXECUTE format('REVOKE ALL ON ALL ROUTINES IN SCHEMA admin FROM %I', grantee);
    END LOOP;
END
$$;
COMMENT ON PROCEDURE admin.restrict_access() IS
    'Revoke every privilege in schema admin from PUBLIC and all roles but the owner; call at the end of each admin migration and after grant scripts';

-- ---------------------------------------------------------------------- admin.api_token
CREATE TABLE admin.api_token (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    token_sha256 bytea NOT NULL UNIQUE CHECK (octet_length(token_sha256) = 32),
    prefix       text NOT NULL CHECK (prefix ~ '^mdh_[a-z2-7]{4}$'),
    scopes       text[] NOT NULL CHECK (
                     cardinality(scopes) >= 1
                     AND scopes <@ ARRAY['read', 'snapshots', 'export']::text[]),
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    revoked_at   timestamptz,
    last_used_at timestamptz,
    last_used_ip inet,
    CONSTRAINT api_token_expiry_after_creation CHECK (expires_at IS NULL OR expires_at > created_at)
);
-- A name is unique among live tokens; a revoked name can be reused by a new token.
CREATE UNIQUE INDEX api_token_live_name_key ON admin.api_token (name) WHERE revoked_at IS NULL;
COMMENT ON TABLE admin.api_token IS 'Named read-API bearer tokens (ADMIN_DESIGN §3.3); only the SHA-256 of a token is stored';
COMMENT ON COLUMN admin.api_token.token_sha256 IS 'SHA-256 of the full token (mdh_<base32>); looked up by the API, then hmac.compare_digest';
COMMENT ON COLUMN admin.api_token.prefix IS 'First 8 characters of the token, for display and secret-scanner matches';
COMMENT ON COLUMN admin.api_token.scopes IS 'read = all of /v1; snapshots = /v1/snapshots*; export = /v1/export/*';
COMMENT ON COLUMN admin.api_token.last_used_at IS 'Updated by the API request-log writer (at most ~1 s behind)';

CALL admin.restrict_access();
