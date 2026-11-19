ALTER TABLE jimu_workflow.jobs
    ADD COLUMN internal_tool_step boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT internal_tool_step_job_budget CHECK (
        NOT internal_tool_step
        OR (max_attempts = 1 AND status IN ('leased', 'done') AND NOT retry_on_lease_loss)
    ),
    ADD UNIQUE (project_id, client_id, execution_id, node_run_id, job_id, internal_tool_step);

CREATE TABLE jimu_workflow.tool_step_attempts (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    parent_node_run_id text NOT NULL,
    parent_job_id text NOT NULL,
    parent_attempt_id text NOT NULL,
    parent_fence bigint NOT NULL CHECK (parent_fence > 0),
    parent_internal boolean GENERATED ALWAYS AS (false) STORED,
    child_node_run_id text NOT NULL,
    child_job_id text NOT NULL,
    child_attempt_id text NOT NULL,
    child_fence bigint NOT NULL CHECK (child_fence > 0),
    child_internal boolean GENERATED ALWAYS AS (true) STORED,
    step_id text NOT NULL CHECK (step_id <> ''),
    invocation_input_digest jimu_workflow.digest NOT NULL,
    bound_input_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, child_node_run_id, child_attempt_id),
    UNIQUE (project_id, client_id, execution_id, child_node_run_id),
    UNIQUE (project_id, client_id, execution_id, parent_node_run_id, parent_attempt_id, step_id),
    UNIQUE (project_id, client_id, execution_id, child_node_run_id, child_attempt_id,
        parent_node_run_id, step_id, bound_input_digest),
    FOREIGN KEY (project_id, client_id, execution_id, parent_node_run_id, parent_job_id, parent_internal)
        REFERENCES jimu_workflow.jobs (project_id, client_id, execution_id, node_run_id, job_id, internal_tool_step),
    FOREIGN KEY (project_id, client_id, execution_id, child_node_run_id, child_job_id, child_internal)
        REFERENCES jimu_workflow.jobs (project_id, client_id, execution_id, node_run_id, job_id, internal_tool_step),
    FOREIGN KEY (project_id, client_id, execution_id, parent_node_run_id, parent_job_id, parent_fence, parent_attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id),
    FOREIGN KEY (project_id, client_id, execution_id, child_node_run_id, child_job_id, child_fence, child_attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id),
    CHECK (parent_node_run_id <> child_node_run_id)
);

CREATE TABLE jimu_workflow.tool_step_completions (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    child_node_run_id text NOT NULL,
    child_attempt_id text NOT NULL,
    parent_node_run_id text NOT NULL,
    step_id text NOT NULL,
    bound_input_digest jimu_workflow.digest NOT NULL,
    attempt_status text NOT NULL CHECK (attempt_status IN ('succeeded', 'failed', 'timed_out')),
    tool_execution_id text NOT NULL,
    result_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, child_node_run_id, child_attempt_id),
    UNIQUE (project_id, client_id, execution_id, tool_execution_id),
    UNIQUE (project_id, client_id, execution_id, child_node_run_id, child_attempt_id,
        parent_node_run_id, step_id, bound_input_digest, result_digest),
    FOREIGN KEY (project_id, client_id, execution_id, child_node_run_id, child_attempt_id,
        parent_node_run_id, step_id, bound_input_digest)
        REFERENCES jimu_workflow.tool_step_attempts (project_id, client_id, execution_id,
            child_node_run_id, child_attempt_id, parent_node_run_id, step_id, bound_input_digest),
    FOREIGN KEY (project_id, client_id, execution_id, child_node_run_id, child_attempt_id, attempt_status)
        REFERENCES jimu_workflow.attempt_results (project_id, client_id, execution_id, node_run_id, attempt_id, status)
);

CREATE UNIQUE INDEX tool_step_success_identity_idx ON jimu_workflow.tool_step_completions
    (project_id, client_id, execution_id, parent_node_run_id, step_id)
    WHERE attempt_status = 'succeeded';

CREATE TABLE jimu_workflow.tool_step_parent_outputs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    parent_node_run_id text NOT NULL,
    parent_attempt_id text NOT NULL,
    step_id text NOT NULL,
    bound_input_digest jimu_workflow.digest NOT NULL,
    child_node_run_id text NOT NULL,
    child_attempt_id text NOT NULL,
    result_digest jimu_workflow.digest NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, parent_node_run_id, parent_attempt_id, step_id),
    FOREIGN KEY (project_id, client_id, execution_id, parent_node_run_id, parent_attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, attempt_id),
    FOREIGN KEY (project_id, client_id, execution_id, child_node_run_id, child_attempt_id,
        parent_node_run_id, step_id, bound_input_digest, result_digest)
        REFERENCES jimu_workflow.tool_step_completions (project_id, client_id, execution_id,
            child_node_run_id, child_attempt_id, parent_node_run_id, step_id, bound_input_digest, result_digest)
);

GRANT SELECT, INSERT ON jimu_workflow.tool_step_attempts,
    jimu_workflow.tool_step_completions, jimu_workflow.tool_step_parent_outputs
    TO jimu_workflow_app;
