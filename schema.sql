BEGIN;

CREATE SCHEMA IF NOT EXISTS cf_word_echo_op;
REVOKE ALL ON SCHEMA cf_word_echo_op FROM PUBLIC;

CREATE TABLE IF NOT EXISTS cf_word_echo_op.migration_receipts (
    receipt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_release text NOT NULL,
    active_release text NOT NULL,
    event_name text NOT NULL CHECK (event_name IN (
        'provenance_verified',
        'staging_smoke',
        'production_candidate_active',
        'production_smoke',
        'rollback_smoke'
    )),
    catalog_rescue_sha256 character(64) NOT NULL,
    strict_bundle_sha256 character(64) NOT NULL,
    repository_source_sha256 character(64) NOT NULL,
    static_asset_sha256 character(64) NOT NULL,
    service_dir text NOT NULL,
    unit_name text NOT NULL,
    health_state text NOT NULL CHECK (health_state IN ('verified', 'healthy')),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_release, event_name)
);

CREATE TABLE IF NOT EXISTS cf_word_echo_op.active_release_attestations (
    attestation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    active_release text NOT NULL,
    catalog_rescue_sha256 character(64) NOT NULL,
    strict_bundle_sha256 character(64) NOT NULL,
    repository_source_sha256 character(64) NOT NULL,
    static_asset_sha256 character(64) NOT NULL,
    health_state text NOT NULL CHECK (health_state = 'healthy'),
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS word_echo_receipts_event_recorded_idx
    ON cf_word_echo_op.migration_receipts(event_name, recorded_at DESC);
CREATE INDEX IF NOT EXISTS word_echo_attestations_recorded_idx
    ON cf_word_echo_op.active_release_attestations(recorded_at DESC);

REVOKE ALL ON ALL TABLES IN SCHEMA cf_word_echo_op FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA cf_word_echo_op FROM PUBLIC;

COMMIT;
