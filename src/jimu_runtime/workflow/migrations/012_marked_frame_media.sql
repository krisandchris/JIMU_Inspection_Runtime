-- A staged N03 frame is private until its exact Object becomes authoritative.
CREATE TABLE jimu_workflow.marked_frame_media (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    object_id text NOT NULL CHECK (object_id <> ''),
    object_sha256 jimu_workflow.digest NOT NULL,
    media_id text NOT NULL CHECK (media_id <> ''),
    store_id text NOT NULL CHECK (store_id <> ''),
    file_key text NOT NULL UNIQUE CHECK (file_key ~ '^[0-9a-f]{64}$'),
    content_sha256 jimu_workflow.digest NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 67108864),
    schema_version text NOT NULL DEFAULT '1.0.0' CHECK (schema_version = '1.0.0'),
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, attempt_id, object_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, attempt_id)
);

CREATE INDEX marked_frame_object_idx
    ON jimu_workflow.marked_frame_media (project_id, client_id, execution_id, object_id);

GRANT SELECT, INSERT ON jimu_workflow.marked_frame_media TO jimu_workflow_app;
