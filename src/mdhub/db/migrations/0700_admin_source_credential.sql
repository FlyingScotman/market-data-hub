-- 0700_admin_source_credential: write-only source credentials store (ADMIN_DESIGN §9, phase E).
-- Migration range 0700-0749 = source credentials store.
--
-- The worker holds an X25519 private key (MDHUB_SECRETS_PRIVATE_KEY, worker .env only) and
-- publishes the public key here on start. The console seals each value to that key (ephemeral
-- X25519, HKDF-SHA256, AES-256-GCM; mdhub.credentials) and stores only the ciphertext: neither
-- the console nor the database can decrypt it. Changes reach the worker by NOTIFY
-- mdhub_credentials (trigger below) and a 60 s poll.

-- ---------------------------------------------------------------- admin.secret_key_public
CREATE TABLE admin.secret_key_public (
    key_id       text PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{16}$'),
    public_key   bytea NOT NULL UNIQUE CHECK (octet_length(public_key) = 32),
    env_names    text[] NOT NULL DEFAULT '{}',
    published_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE admin.secret_key_public IS 'X25519 public keys of the worker; the latest published_at is the key the console seals to';
COMMENT ON COLUMN admin.secret_key_public.key_id IS 'First 8 bytes of the SHA-256 of the public key, hex';
COMMENT ON COLUMN admin.secret_key_public.env_names IS 'Registry names set in the worker environment (.env) when it published; names only, never values';

-- ---------------------------------------------------------------- admin.source_credential
CREATE TABLE admin.source_credential (
    name          text PRIMARY KEY CHECK (name IN (
                      'MOEX_API_KEY', 'MOEX_PASSPORT_LOGIN', 'MOEX_PASSPORT_PASSWORD',
                      'RATESCALC_USERNAME', 'RATESCALC_PASSWORD', 'CBONDS_LOGIN',
                      'CBONDS_PASSWORD', 'OTC_MONITOR_TOKEN', 'MDHUB_HTTP_PROXY')),
    ciphertext    bytea NOT NULL CHECK (octet_length(ciphertext) > 61),
    key_id        text NOT NULL CHECK (key_id ~ '^[0-9a-f]{16}$'),
    set_by        text,
    set_at        timestamptz NOT NULL DEFAULT now(),
    verified_at   timestamptz,
    verify_result text CHECK (verify_result IN ('ok', 'rejected', 'unreachable'))
);
COMMENT ON TABLE admin.source_credential IS 'Source credentials sealed to the worker public key (ADMIN_DESIGN §9); a value here wins over the worker .env';
COMMENT ON COLUMN admin.source_credential.name IS 'Environment name from the fixed registry mdhub.credentials.REGISTRY';
COMMENT ON COLUMN admin.source_credential.ciphertext IS 'Sealed box: 0x01 | ephemeral X25519 public key (32) | nonce (12) | AES-256-GCM ciphertext and tag; AAD = name and key id';
COMMENT ON COLUMN admin.source_credential.key_id IS 'admin.secret_key_public.key_id sealed to (no FK: a row under an unknown key is shown as "re-enter" and never used)';
COMMENT ON COLUMN admin.source_credential.verify_result IS 'Last source_probe of the credential''s source by the worker while this value was set';

CREATE FUNCTION admin.notify_credentials() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('mdhub_credentials', TG_OP);
    RETURN NULL;
END
$$;
CREATE TRIGGER source_credential_notify
    AFTER INSERT OR DELETE OR UPDATE OF ciphertext, key_id ON admin.source_credential
    FOR EACH STATEMENT EXECUTE FUNCTION admin.notify_credentials();

CALL admin.restrict_access();
