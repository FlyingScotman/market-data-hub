-- Timestamp-aligned indicative input facts. Source captures may expire; the selected
-- values, clocks, specifications and capture hashes remain in the immutable document.
INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('ntpro_public', 'NTPro public FX snapshots', 'https://ntprog.ru',
     'Public indicative quotes; row time is not an executable side-tick clock')
ON CONFLICT (code) DO NOTHING;

CREATE TABLE md.fx_input_pair_group (
    run_id         bigint NOT NULL REFERENCES ops.job_run (id),
    currency       text NOT NULL CHECK (currency IN ('USD', 'EUR', 'CNY')),
    known_at       timestamptz NOT NULL,
    document       jsonb NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    ingested_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, currency)
);
CREATE INDEX fx_input_pair_group_known_idx ON md.fx_input_pair_group (currency, known_at DESC);
COMMENT ON TABLE md.fx_input_pair_group IS
    'Append-only per-run FX input groups: selected values and source clocks survive raw capture retention';

-- A separate transaction reads the committed terminal job row before observing this clock.
-- observed_at proves visibility no later than that instant; it is not a commit timestamp.
CREATE TABLE md.fx_input_pair_completion_evidence (
    run_id      bigint NOT NULL REFERENCES ops.job_run (id),
    status      text NOT NULL CHECK (status IN ('succeeded', 'partial', 'failed', 'skipped')),
    finished_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, status, finished_at)
);

-- One immutable decision (including absence) per full market cut identity. It is written
-- under the existing publication lock, in the same transaction as the published snapshot.
CREATE TABLE md.fx_input_pair_cut_selection (
    business_date  date NOT NULL,
    cut_type       text NOT NULL,
    cut_ts         timestamptz NOT NULL,
    known_at       timestamptz NOT NULL,
    document       jsonb NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (business_date, cut_type, cut_ts, known_at)
);
