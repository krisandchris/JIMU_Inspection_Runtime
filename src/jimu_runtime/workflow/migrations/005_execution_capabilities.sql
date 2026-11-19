CREATE TABLE jimu_workflow.execution_capabilities (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    input_digest jimu_workflow.digest NOT NULL,
    record_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id)
);

GRANT SELECT, INSERT ON jimu_workflow.execution_capabilities TO jimu_workflow_app;
