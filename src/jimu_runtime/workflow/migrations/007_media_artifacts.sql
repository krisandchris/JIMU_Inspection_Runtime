-- Scope-bound local content references. The application can only append.
CREATE TABLE jimu_workflow.original_media (
    project_id text NOT NULL,
    client_id text NOT NULL,
    media_id text NOT NULL,
    record json NOT NULL,
    file_key text GENERATED ALWAYS AS (pg_catalog.split_part(record -> 'storage' ->> 'object_key', '/', 2)) STORED NOT NULL,
    UNIQUE (file_key),
    CHECK (file_key ~ '^[0-9a-f]{64}$'
        AND record -> 'storage' ->> 'object_key' = 'media-v1/' || file_key),
    PRIMARY KEY (project_id, client_id, media_id),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record -> 'media_ref' ->> 'media_id') IS NOT DISTINCT FROM media_id),
    CHECK ((record -> 'stage' ->> 'stage') IS NOT DISTINCT FROM 'finalized')
);

-- Staged transport/Tool bytes carry identity and integrity metadata only.
-- A journal receipt or an exact Attempt output remains the source of authority.
CREATE TABLE jimu_workflow.dispatch_media (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    dispatch_id text NOT NULL,
    role text NOT NULL CHECK (role IN (
        'model_raw', 'model_normalized', 'wheel_mask', 'tactile_paving_mask', 'geometry'
    )),
    schema_version text NOT NULL DEFAULT '1.0.0' CHECK (schema_version = '1.0.0'),
    store_id text NOT NULL CHECK (store_id <> ''),
    file_key text NOT NULL UNIQUE CHECK (file_key ~ '^[0-9a-f]{64}$'),
    content_sha256 jimu_workflow.digest NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    wire_sha256 jimu_workflow.digest,
    wire_size bigint CHECK (wire_size > 0),
    evidence_id text,
    evidence_claim_sha256 jimu_workflow.digest,
    staged_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (project_id, client_id, execution_id, dispatch_id, role),
    CHECK (
        (role = 'model_raw' AND evidence_id IS NULL AND evidence_claim_sha256 IS NULL AND
         content_sha256 IS NOT DISTINCT FROM wire_sha256 AND
         byte_size IS NOT DISTINCT FROM wire_size)
        OR (role = 'model_normalized' AND evidence_id IS NULL AND evidence_claim_sha256 IS NULL AND
            wire_sha256 IS NOT NULL AND wire_size IS NOT NULL)
        OR (role IN ('wheel_mask', 'tactile_paving_mask', 'geometry') AND
            evidence_id IS NOT NULL AND evidence_id <> '' AND evidence_claim_sha256 IS NOT NULL AND
            wire_sha256 IS NULL AND wire_size IS NULL)
    ),
    FOREIGN KEY (project_id, client_id, execution_id, dispatch_id)
        REFERENCES jimu_workflow.dispatch_intents
            (project_id, client_id, execution_id, dispatch_id)
);

CREATE INDEX dispatch_media_evidence_idx
    ON jimu_workflow.dispatch_media (project_id, client_id, execution_id, evidence_id)
    WHERE evidence_id IS NOT NULL;

GRANT SELECT, INSERT ON jimu_workflow.original_media, jimu_workflow.dispatch_media
    TO jimu_workflow_app;
