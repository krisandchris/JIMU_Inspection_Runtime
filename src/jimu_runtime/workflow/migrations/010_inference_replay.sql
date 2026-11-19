CREATE TABLE jimu_workflow.inference_replay_runs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    idempotency_key bytea NOT NULL CHECK (octet_length(idempotency_key) BETWEEN 1 AND 512),
    request_digest jimu_workflow.digest NOT NULL,
    issuer text NOT NULL CHECK (issuer <> ''),
    subject text NOT NULL CHECK (subject <> ''),
    replay_run_id text NOT NULL,
    source_execution_id text NOT NULL,
    target_execution_id text NOT NULL,
    capture_execution_id text NOT NULL,
    capture_snapshot_digest jimu_workflow.digest NOT NULL,
    task_id text NOT NULL,
    record_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, idempotency_key),
    UNIQUE (project_id, client_id, replay_run_id),
    UNIQUE (project_id, client_id, target_execution_id),
    CHECK (source_execution_id <> target_execution_id),
    CHECK (capture_execution_id <> target_execution_id),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record ->> 'kind') IS NOT DISTINCT FROM 'inference'),
    CHECK ((record ->> 'replay_run_id') IS NOT DISTINCT FROM replay_run_id),
    CHECK ((record #>> '{request,source,execution_ref,record_id}') IS NOT DISTINCT FROM source_execution_id),
    CHECK ((record #>> '{request,source,task_id}') IS NOT DISTINCT FROM task_id),
    CHECK ((record #>> '{target_execution_ref,record_id}') IS NOT DISTINCT FROM target_execution_id),
    FOREIGN KEY (project_id, client_id, source_execution_id, task_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id, task_id),
    FOREIGN KEY (project_id, client_id, capture_execution_id, task_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id, task_id),
    FOREIGN KEY (project_id, client_id, target_execution_id, task_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id, task_id)
);

GRANT SELECT, INSERT ON jimu_workflow.inference_replay_runs TO jimu_workflow_app;
