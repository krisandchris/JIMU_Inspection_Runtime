-- Scoring evidence and its owning run are accepted in one immutable row.
CREATE TABLE jimu_workflow.artifact_replay_runs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    replay_run_id text NOT NULL,
    evidence_id text NOT NULL,
    source_execution_id text NOT NULL,
    idempotency_key bytea NOT NULL
        CHECK (octet_length(idempotency_key) BETWEEN 1 AND 2048),
    request_digest jimu_workflow.digest NOT NULL,
    run_digest jimu_workflow.digest NOT NULL,
    evidence_digest jimu_workflow.digest NOT NULL,
    run_record json NOT NULL,
    evidence_record json NOT NULL,
    PRIMARY KEY (project_id, client_id, replay_run_id),
    UNIQUE (project_id, client_id, idempotency_key),
    UNIQUE (project_id, client_id, evidence_id),
    FOREIGN KEY (project_id, client_id, source_execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id)
);

GRANT SELECT, INSERT ON jimu_workflow.artifact_replay_runs TO jimu_workflow_app;
