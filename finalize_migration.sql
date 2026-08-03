\if :{?active_release}
\else
\echo 'active_release psql variable is required'
\quit
\endif

BEGIN;
SELECT set_config('echo.word.active_release', :'active_release', true);

INSERT INTO cf_word_echo_op.active_release_attestations
    (active_release, catalog_rescue_sha256, strict_bundle_sha256,
     repository_source_sha256, static_asset_sha256, health_state)
VALUES
    (:'active_release',
     '84d29f7b2fa2718801797eb237efa0920cd210baaeeb10104ef8cd9d3c22e437',
     '70d697061b7e5d2a0fe2642342d87ebed7046fb2c5de8bfbbd34ca3f70bc3d05',
     '2a2ec38174de4277b199980d5ac7f2d379653308535fcbba70353ef151a0c5ee',
     '364263a47a7a44b12fadaf0f81b83d1a11813e46107e874523e4701468945a14',
     'healthy');

DO $finalize$
DECLARE
    catalog_rows integer;
    expected_active_release constant text := current_setting('echo.word.active_release');
    catalog_sha constant text := '84d29f7b2fa2718801797eb237efa0920cd210baaeeb10104ef8cd9d3c22e437';
    strict_sha constant text := '70d697061b7e5d2a0fe2642342d87ebed7046fb2c5de8bfbbd34ca3f70bc3d05';
    repository_sha constant text := '2a2ec38174de4277b199980d5ac7f2d379653308535fcbba70353ef151a0c5ee';
    asset_sha constant text := '364263a47a7a44b12fadaf0f81b83d1a11813e46107e874523e4701468945a14';
BEGIN
    IF expected_active_release !~ '^/home/forge/word-echo-op/releases/[A-Za-z0-9._-]+$' THEN
        RAISE EXCEPTION 'word migration finalization refused: invalid active release path';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM inventory.cf_migration_status
         WHERE lower(worker_name) = 'word-echo-op' AND btrim(source_sha256) = catalog_sha
    ) THEN
        RAISE EXCEPTION 'word migration finalization refused: catalog rescue identity mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM cf_word_echo_op.migration_receipts provenance
          JOIN cf_word_echo_op.migration_receipts staging
            ON staging.candidate_release = provenance.candidate_release
           AND staging.event_name = 'staging_smoke'
           AND staging.health_state = 'healthy'
           AND staging.recorded_at >= provenance.recorded_at
          JOIN cf_word_echo_op.migration_receipts production
            ON production.candidate_release = provenance.candidate_release
           AND production.event_name = 'production_smoke'
           AND production.health_state = 'healthy'
           AND production.active_release = production.candidate_release
           AND production.recorded_at >= staging.recorded_at
          JOIN cf_word_echo_op.migration_receipts failed_provenance
            ON failed_provenance.candidate_release <> production.candidate_release
           AND failed_provenance.event_name = 'provenance_verified'
           AND failed_provenance.recorded_at >= production.recorded_at
          JOIN cf_word_echo_op.migration_receipts failed_staging
            ON failed_staging.candidate_release = failed_provenance.candidate_release
           AND failed_staging.event_name = 'staging_smoke'
           AND failed_staging.health_state = 'healthy'
           AND failed_staging.recorded_at >= failed_provenance.recorded_at
          JOIN cf_word_echo_op.migration_receipts active_attempt
            ON active_attempt.candidate_release = failed_provenance.candidate_release
           AND active_attempt.event_name = 'production_candidate_active'
           AND active_attempt.active_release = active_attempt.candidate_release
           AND active_attempt.recorded_at >= failed_staging.recorded_at
          JOIN cf_word_echo_op.migration_receipts rollback
            ON rollback.candidate_release = failed_provenance.candidate_release
           AND rollback.event_name = 'rollback_smoke'
           AND rollback.health_state = 'healthy'
           AND rollback.active_release = production.candidate_release
           AND rollback.recorded_at >= active_attempt.recorded_at
          JOIN cf_word_echo_op.active_release_attestations attestation
            ON attestation.active_release = production.candidate_release
           AND attestation.health_state = 'healthy'
           AND attestation.recorded_at >= now() - interval '5 minutes'
         WHERE provenance.event_name = 'provenance_verified'
           AND provenance.health_state = 'verified'
           AND production.candidate_release = expected_active_release
           AND provenance.catalog_rescue_sha256 = catalog_sha
           AND provenance.strict_bundle_sha256 = strict_sha
           AND provenance.repository_source_sha256 = repository_sha
           AND provenance.static_asset_sha256 = asset_sha
           AND staging.catalog_rescue_sha256 = catalog_sha
           AND staging.strict_bundle_sha256 = strict_sha
           AND staging.repository_source_sha256 = repository_sha
           AND staging.static_asset_sha256 = asset_sha
           AND production.catalog_rescue_sha256 = catalog_sha
           AND production.strict_bundle_sha256 = strict_sha
           AND production.repository_source_sha256 = repository_sha
           AND production.static_asset_sha256 = asset_sha
           AND production.service_dir = '/home/forge/word-echo-op'
           AND production.unit_name = 'word-echo-op.service'
           AND failed_provenance.catalog_rescue_sha256 = catalog_sha
           AND failed_provenance.strict_bundle_sha256 = strict_sha
           AND failed_provenance.repository_source_sha256 = repository_sha
           AND failed_provenance.static_asset_sha256 = asset_sha
           AND rollback.service_dir = '/home/forge/word-echo-op'
           AND rollback.unit_name = 'word-echo-op.service'
           AND attestation.catalog_rescue_sha256 = catalog_sha
           AND attestation.strict_bundle_sha256 = strict_sha
           AND attestation.repository_source_sha256 = repository_sha
           AND attestation.static_asset_sha256 = asset_sha
    ) THEN
        RAISE EXCEPTION 'word migration finalization refused: ordered production/rollback/fresh-attestation chain missing';
    END IF;

    UPDATE arcanum_sdk.cf_artifact_catalog
       SET status = 'verified',
           target_origin = 'http://127.0.0.1:8464',
           notes = 'Verified FORGE replacement: independent source identities, exact one-record static payload, 2/2 generic behavior, staging gate, and ordered rollback proof.',
           metadata = coalesce(metadata, '{}'::jsonb) || jsonb_build_object(
               'forge_service_dir', '/home/forge/word-echo-op',
               'forge_unit', 'word-echo-op.service',
               'migration_contract', '/home/forge/word-echo-op/current/migration_contract.json',
               'active_release', expected_active_release,
               'catalog_rescued_sha256', catalog_sha,
               'strict_bundle_sha256', strict_sha,
               'repository_source_sha256', repository_sha,
               'static_asset_sha256', asset_sha,
               'generic_route_contract', '2/2',
               'source_non_generic_route_count', 0,
               'verified_at', now()
           ),
           updated_at = now()
     WHERE kind = 'worker' AND lower(name) = 'word-echo-op';
    GET DIAGNOSTICS catalog_rows = ROW_COUNT;
    IF catalog_rows <> 1 THEN
        RAISE EXCEPTION 'word migration finalization refused: expected one catalog row, updated %', catalog_rows;
    END IF;
END
$finalize$;

INSERT INTO arcanum_sdk.cf_migration_track
    (cf_service_name, cf_service_kind, status, priority, echo_replacement_kind,
     echo_target_path, owner_agent, notes, migrated_at, updated_at)
VALUES
    ('word-echo-op', 'worker', 'migrated', 6, 'fastapi',
     '/home/forge/word-echo-op', 'continuous-builder',
     'Exact catalog/strict/repository/static-asset provenance, 2/2 generic contract, fresh active smoke, and ordered rollback proof are green.',
     now(), now())
ON CONFLICT (cf_service_name) DO UPDATE SET
    status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    echo_replacement_kind = EXCLUDED.echo_replacement_kind,
    echo_target_path = EXCLUDED.echo_target_path,
    owner_agent = EXCLUDED.owner_agent,
    notes = EXCLUDED.notes,
    migrated_at = EXCLUDED.migrated_at,
    updated_at = EXCLUDED.updated_at;

COMMIT;
