-- 0620_admin_auth: users, credentials, sessions, throttling and the audit trail of the admin
-- console (ADMIN_DESIGN §3.1, §5). Code: mdhub.admin.
--
-- Deviations from §3.1: the user table is admin.user_account ("user" is a reserved word) and
-- login is lower-case text with a CHECK instead of citext (no extension needed). Sessions carry
-- a stage (anonymous login page, after the password, during enrollment, full) and the pending
-- WebAuthn challenge, so no state lives in the cookie.

-- -------------------------------------------------------------------- admin.user_account
CREATE TABLE admin.user_account (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    login               text NOT NULL UNIQUE CHECK (login ~ '^[a-z0-9][a-z0-9._@-]{0,63}$'),
    display_name        text NOT NULL DEFAULT '' CHECK (length(display_name) <= 128),
    role                text NOT NULL CHECK (role IN ('viewer', 'operator', 'admin')),
    password_hash       text,
    passkey_only        boolean NOT NULL DEFAULT false,
    webauthn_user_id    bytea NOT NULL UNIQUE CHECK (octet_length(webauthn_user_id) = 32),
    disabled_at         timestamptz,
    password_changed_at timestamptz,
    created_by          text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE admin.user_account IS 'Admin console users (ADMIN_DESIGN §3.1); created by `mdhub admin create-user` or an admin';
COMMENT ON COLUMN admin.user_account.password_hash IS 'argon2id (argon2-cffi); NULL until the enrollment link was used';
COMMENT ON COLUMN admin.user_account.passkey_only IS 'User allows a passkey on its own (no password) to log in';
COMMENT ON COLUMN admin.user_account.webauthn_user_id IS 'Random WebAuthn user handle (never the login or id)';

-- ------------------------------------------------------------ admin.webauthn_credential
CREATE TABLE admin.webauthn_credential (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES admin.user_account (id) ON DELETE CASCADE,
    credential_id bytea NOT NULL UNIQUE,
    public_key    bytea NOT NULL,
    sign_count    bigint NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
    transports    text[] NOT NULL DEFAULT '{}',
    aaguid        text,
    name          text NOT NULL DEFAULT 'passkey' CHECK (length(name) BETWEEN 1 AND 64),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    suspect_at    timestamptz
);
CREATE INDEX webauthn_credential_user_idx ON admin.webauthn_credential (user_id);
COMMENT ON COLUMN admin.webauthn_credential.public_key IS 'COSE public key from the attestation';
COMMENT ON COLUMN admin.webauthn_credential.suspect_at IS 'Set when an assertion carried a sign counter that did not increase (possible clone); the credential is refused from then on';

-- -------------------------------------------------------------------- admin.totp_secret
CREATE TABLE admin.totp_secret (
    user_id      bigint PRIMARY KEY REFERENCES admin.user_account (id) ON DELETE CASCADE,
    secret_enc   bytea NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,
    last_step    bigint
);
COMMENT ON COLUMN admin.totp_secret.secret_enc IS 'AES-256-GCM(MDHUB_ADMIN_SECRET_KEY): 12-byte nonce || ciphertext+tag of the raw secret';
COMMENT ON COLUMN admin.totp_secret.last_step IS 'Last accepted RFC 6238 time step (replay guard: a step is accepted once)';

-- ------------------------------------------------------------------ admin.recovery_code
CREATE TABLE admin.recovery_code (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES admin.user_account (id) ON DELETE CASCADE,
    code_hash  text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    used_at    timestamptz
);
CREATE INDEX recovery_code_user_idx ON admin.recovery_code (user_id) WHERE used_at IS NULL;
COMMENT ON TABLE admin.recovery_code IS 'One-time recovery codes (10 per generation), argon2id hashes';

-- --------------------------------------------------------------------- admin.enrollment
CREATE TABLE admin.enrollment (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_sha256 bytea NOT NULL UNIQUE CHECK (octet_length(token_sha256) = 32),
    user_id      bigint NOT NULL REFERENCES admin.user_account (id) ON DELETE CASCADE,
    purpose      text NOT NULL CHECK (purpose IN ('invite', 'reset')),
    created_by   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    used_at      timestamptz
);
CREATE INDEX enrollment_user_idx ON admin.enrollment (user_id);
COMMENT ON TABLE admin.enrollment IS 'One-time invite / reset links (15 min); only the SHA-256 of the link token is stored';

-- ------------------------------------------------------------------------ admin.session
CREATE TABLE admin.session (
    id_sha256            bytea PRIMARY KEY CHECK (octet_length(id_sha256) = 32),
    user_id              bigint REFERENCES admin.user_account (id) ON DELETE CASCADE,
    stage                text NOT NULL CHECK (stage IN ('anonymous', 'password', 'enroll', 'full')),
    enrollment_id        bigint REFERENCES admin.enrollment (id) ON DELETE CASCADE,
    csrf_token           text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz NOT NULL,
    ip                   inet,
    user_agent           text,
    mfa_at               timestamptz,
    step_up_at           timestamptz,
    revoked_at           timestamptz,
    webauthn_challenge   bytea,
    challenge_expires_at timestamptz,
    CONSTRAINT session_user_unless_anonymous CHECK ((stage = 'anonymous') = (user_id IS NULL))
);
CREATE INDEX session_user_idx ON admin.session (user_id) WHERE revoked_at IS NULL;
CREATE INDEX session_expires_idx ON admin.session (expires_at);
COMMENT ON TABLE admin.session IS 'Console sessions: only the SHA-256 of the 256-bit cookie value is stored; idle 30 min, absolute 12 h';
COMMENT ON COLUMN admin.session.stage IS 'anonymous (login page), password (password ok, second factor pending), enroll (enrollment link used), full';
COMMENT ON COLUMN admin.session.csrf_token IS 'Synchronizer token for unsafe requests of this session';

-- ------------------------------------------------------------------ admin.login_attempt
CREATE TABLE admin.login_attempt (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT clock_timestamp(),
    login   text,
    ip      inet,
    outcome text NOT NULL CHECK (outcome IN ('success', 'failure', 'throttled', 'unlocked'))
);
CREATE INDEX login_attempt_login_ts_idx ON admin.login_attempt (login, ts DESC);
CREATE INDEX login_attempt_ip_ts_idx ON admin.login_attempt (ip, ts DESC);
CREATE INDEX login_attempt_ts_idx ON admin.login_attempt (ts);
COMMENT ON TABLE admin.login_attempt IS 'Login outcomes per typed login and IP, for throttling and lockout (kept 30 days)';
COMMENT ON COLUMN admin.login_attempt.outcome IS 'unlocked = an admin unlocked the login: failures before it no longer count';

-- ---------------------------------------------------------------------- admin.audit_log
CREATE TABLE admin.audit_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT clock_timestamp(),
    user_id     bigint,
    login       text,
    session_ref text,
    ip          inet,
    action      text NOT NULL,
    target      text,
    details     jsonb NOT NULL DEFAULT '{}'::jsonb,
    outcome     text NOT NULL DEFAULT 'ok' CHECK (outcome IN ('ok', 'denied', 'failed'))
);
CREATE INDEX audit_log_ts_idx ON admin.audit_log (ts DESC);
CREATE INDEX audit_log_user_ts_idx ON admin.audit_log (user_id, ts DESC);
COMMENT ON TABLE admin.audit_log IS 'Append-only audit trail of the console and `mdhub admin` (no secrets); UPDATE/DELETE/TRUNCATE are revoked and refused by trigger';
COMMENT ON COLUMN admin.audit_log.user_id IS 'admin.user_account.id of the actor (no FK: the trail outlives users); NULL for the CLI';
COMMENT ON COLUMN admin.audit_log.session_ref IS 'First 12 hex digits of the session id SHA-256 (correlates entries, cannot be used as a session)';

CREATE FUNCTION admin.audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'admin.audit_log is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON admin.audit_log
    FOR EACH ROW EXECUTE FUNCTION admin.audit_log_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON admin.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION admin.audit_log_append_only();
-- The owner is the app role: take the privileges away from it as well (the trigger also stops
-- superusers, who bypass privileges).
DO $$
BEGIN
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON admin.audit_log FROM %I', current_user);
END
$$;

-- -------------------------------------------- ops.job_run.trigger: runs started in the console
ALTER TABLE ops.job_run DROP CONSTRAINT IF EXISTS job_run_trigger_chk;
ALTER TABLE ops.job_run ADD CONSTRAINT job_run_trigger_chk CHECK (
    trigger IS NULL OR trigger IN ('schedule', 'catch_up', 'backfill', 'cli', 'api', 'admin', 'other'));
COMMENT ON COLUMN ops.job_run.trigger IS
    'Who started the run: schedule (scheduler fire, incl. lock_busy skips), catch_up (quality '
    're-run of a fire skipped with lock_busy), backfill (backfill task run), cli (mdhub run / '
    'mdhub ops ...), api, admin (operator in the admin console), other; NULL = recorded before '
    '0421. Job health counts only schedule / catch_up (and NULL) runs as "the fire ran"';

CALL admin.restrict_access();
