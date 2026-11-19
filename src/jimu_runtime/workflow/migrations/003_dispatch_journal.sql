CREATE TABLE jimu_workflow.dispatch_operations (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    operation_id text NOT NULL CHECK (operation_id <> ''),
    input_digest jimu_workflow.digest NOT NULL,
    response_contract_digest jimu_workflow.digest NOT NULL,
    target_digest jimu_workflow.digest NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, operation_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id)
);

CREATE TABLE jimu_workflow.dispatch_intents (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    operation_node_run_id text NOT NULL,
    operation_id text NOT NULL,
    dispatch_id text NOT NULL CHECK (dispatch_id <> ''),
    attempt_id text NOT NULL,
    job_id text NOT NULL,
    fence bigint NOT NULL,
    lease_owner text NOT NULL,
    send_admitted boolean NOT NULL,
    record json NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (project_id, client_id, execution_id, dispatch_id),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record ->> 'dispatch_id') IS NOT DISTINCT FROM dispatch_id),
    CHECK ((record ->> 'operation_id') IS NOT DISTINCT FROM operation_id),
    CHECK (operation_node_run_id = COALESCE(record -> 'target' ->> 'parent_node_run_id', node_run_id)),
    FOREIGN KEY (project_id, client_id, execution_id, operation_node_run_id, operation_id)
        REFERENCES jimu_workflow.dispatch_operations
            (project_id, client_id, execution_id, node_run_id, operation_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id, lease_owner)
        REFERENCES jimu_workflow.attempts
            (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id, lease_owner)
);

CREATE INDEX dispatch_intents_operation_idx ON jimu_workflow.dispatch_intents
    (project_id, client_id, execution_id, node_run_id, operation_id, dispatch_id COLLATE "C");

CREATE INDEX dispatch_parent_operation_idx ON jimu_workflow.dispatch_intents
    (project_id, client_id, execution_id, operation_node_run_id, operation_id, dispatch_id COLLATE "C")
    WHERE operation_node_run_id <> node_run_id;

CREATE INDEX dispatch_admitted_node_idx ON jimu_workflow.dispatch_intents
    (project_id, client_id, execution_id, node_run_id, operation_id)
    WHERE send_admitted;

CREATE UNIQUE INDEX dispatch_one_send_per_attempt_idx ON jimu_workflow.dispatch_intents
    (project_id, client_id, execution_id, node_run_id, operation_id, attempt_id)
    WHERE send_admitted;

CREATE TABLE jimu_workflow.dispatch_results (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    dispatch_id text NOT NULL,
    late boolean NOT NULL,
    record json NOT NULL,
    boundary text GENERATED ALWAYS AS (record ->> 'boundary') STORED NOT NULL
        CHECK (boundary IN ('not_dispatched', 'sent_unknown', 'acknowledged')),
    recorded_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (project_id, client_id, execution_id, dispatch_id, late),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK (NOT late OR boundary = 'acknowledged'),
    FOREIGN KEY (project_id, client_id, execution_id, dispatch_id)
        REFERENCES jimu_workflow.dispatch_intents (project_id, client_id, execution_id, dispatch_id)
);

GRANT SELECT, INSERT ON
    jimu_workflow.dispatch_operations,
    jimu_workflow.dispatch_intents,
    jimu_workflow.dispatch_results
    TO jimu_workflow_app;
