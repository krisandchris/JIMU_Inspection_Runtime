-- Bound compiled prompt and response Schema bytes; N04 remains their EvidenceAsset owner.
CREATE TABLE jimu_workflow.observation_request_media (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    evidence_id text NOT NULL CHECK (evidence_id <> ''),
    store_id text NOT NULL CHECK (store_id <> ''),
    file_key text NOT NULL UNIQUE CHECK (file_key ~ '^[0-9a-f]{64}$'),
    content_sha256 jimu_workflow.digest NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 1048576),
    staged_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    schema_version text NOT NULL DEFAULT '1.0.0' CHECK (schema_version = '1.0.0'),
    PRIMARY KEY (project_id, client_id, execution_id, evidence_id),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id)
);

-- A tombstone retains the original immutable identity while releasing orphan bytes.
CREATE TABLE jimu_workflow.observation_request_media_reclaims (
    file_key text PRIMARY KEY
        REFERENCES jimu_workflow.observation_request_media (file_key),
    reclaimed_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

GRANT SELECT, INSERT ON jimu_workflow.observation_request_media,
    jimu_workflow.observation_request_media_reclaims TO jimu_workflow_app;
