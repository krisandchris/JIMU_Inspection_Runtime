-- An N05 parent can release its worker once while the paired N04 is unfinished.
-- Its immutable attempt history contains the superseded phase in addition to
-- the configured budget of real attempts.
ALTER TABLE jimu_workflow.jobs
    ADD COLUMN observation_wait_node_id text,
    ADD CONSTRAINT jobs_observation_wait_edge
        FOREIGN KEY (project_id, client_id, execution_id, observation_wait_node_id, node_run_id)
        REFERENCES jimu_workflow.successors
            (project_id, client_id, execution_id, upstream_node_run_id, downstream_node_run_id),
    ADD CONSTRAINT jobs_observation_wait_external CHECK (
        observation_wait_node_id IS NULL OR (NOT internal_tool_step AND attempt_count > 0)
    ),
    DROP CONSTRAINT jobs_check,
    ADD CONSTRAINT jobs_attempt_count_check CHECK (
        attempt_count BETWEEN 0 AND max_attempts + (observation_wait_node_id IS NOT NULL)::integer
    );

ALTER TABLE jimu_workflow.attempts
    DROP CONSTRAINT attempts_attempt_number_check,
    ADD CONSTRAINT attempts_attempt_number_check CHECK (attempt_number BETWEEN 1 AND 1025);

GRANT UPDATE (observation_wait_node_id) ON jimu_workflow.jobs TO jimu_workflow_app;
