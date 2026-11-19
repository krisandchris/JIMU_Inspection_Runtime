CREATE DOMAIN jimu_workflow.digest AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TABLE jimu_workflow.intake_intents (
    project_id text NOT NULL,
    client_id text NOT NULL,
    request_id text NOT NULL,
    idempotency_key bytea NOT NULL,
    submission_digest jimu_workflow.digest NOT NULL,
    submission json NOT NULL,
    issuer text NOT NULL CHECK (issuer <> ''),
    subject text NOT NULL CHECK (subject <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL DEFAULT 'pending',
    failure_code text,
    task_id text,
    execution_id text,
    PRIMARY KEY (project_id, client_id, request_id),
    UNIQUE (project_id, client_id, idempotency_key),
    CHECK (
        (status = 'pending' AND failure_code IS NULL AND task_id IS NULL AND execution_id IS NULL)
        OR (status = 'accepted' AND failure_code IS NULL AND task_id IS NOT NULL AND execution_id IS NOT NULL)
        OR (status = 'failed' AND failure_code IS NOT NULL AND task_id IS NULL AND execution_id IS NULL)
    )
);

CREATE INDEX intake_pending_idx
    ON jimu_workflow.intake_intents (project_id, client_id, request_id COLLATE "C")
    WHERE status = 'pending';

CREATE TABLE jimu_workflow.tasks (
    project_id text NOT NULL,
    client_id text NOT NULL,
    task_id text NOT NULL,
    request_id text NOT NULL,
    release_id text NOT NULL,
    bundle_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted', 'queued', 'running', 'completed', 'partial_failed', 'failed', 'cancelled')),
    PRIMARY KEY (project_id, client_id, task_id),
    UNIQUE (project_id, client_id, request_id),
    UNIQUE (project_id, client_id, request_id, task_id),
    UNIQUE (project_id, client_id, task_id, release_id, bundle_digest),
    FOREIGN KEY (project_id, client_id, request_id)
        REFERENCES jimu_workflow.intake_intents (project_id, client_id, request_id)
);

CREATE TABLE jimu_workflow.executions (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    task_id text NOT NULL,
    release_id text NOT NULL,
    bundle_digest jimu_workflow.digest NOT NULL,
    execution_policy_ref json NOT NULL,
    scheduling_limits json NOT NULL,
    scheduling_limits_version text NOT NULL,
    scheduling_limits_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    state json NOT NULL,
    status text NOT NULL CHECK (status IN ('initialized', 'running', 'completed', 'partial_failed', 'failed', 'cancelled')),
    next_event_number bigint NOT NULL DEFAULT 1 CHECK (next_event_number > 0),
    cancel_requested_at timestamptz,
    PRIMARY KEY (project_id, client_id, execution_id),
    UNIQUE (project_id, client_id, execution_id, task_id),
    FOREIGN KEY (project_id, client_id, task_id)
        REFERENCES jimu_workflow.tasks (project_id, client_id, task_id)
);

ALTER TABLE jimu_workflow.intake_intents
    ADD CONSTRAINT intent_task_fk
        FOREIGN KEY (project_id, client_id, request_id, task_id)
        REFERENCES jimu_workflow.tasks (project_id, client_id, request_id, task_id),
    ADD CONSTRAINT intent_execution_fk
        FOREIGN KEY (project_id, client_id, execution_id, task_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id, task_id);

CREATE TABLE jimu_workflow.node_runs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    node_id text NOT NULL CHECK (node_id IN ('N01', 'N02', 'N03', 'N04', 'N05', 'N06')),
    branch_key text NOT NULL CHECK (branch_key <> ''),
    input_digest jimu_workflow.digest NOT NULL,
    predecessor_id text,
    record json NOT NULL,
    state json NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'ready', 'running', 'succeeded', 'failed', 'blocked', 'skipped', 'cancelled')),
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id),
    UNIQUE (project_id, client_id, execution_id, node_id, branch_key),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id),
    FOREIGN KEY (project_id, client_id, execution_id, predecessor_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id),
    CHECK (predecessor_id IS DISTINCT FROM node_run_id)
);

CREATE TABLE jimu_workflow.jobs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    job_id text NOT NULL,
    status text NOT NULL DEFAULT 'ready',
    fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
    lease_owner text,
    lease_until timestamptz,
    ready_at timestamptz NOT NULL,
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 1024),
    timeout_ms integer NOT NULL CHECK (timeout_ms > 0),
    retry_backoff_ms integer[] NOT NULL,
    retryable_error_classes text[] NOT NULL,
    retry_on_lease_loss boolean NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND max_attempts),
    current_attempt_id text,
    attempt_deadline timestamptz,
    PRIMARY KEY (project_id, client_id, execution_id, job_id),
    UNIQUE (project_id, client_id, execution_id, node_run_id),
    UNIQUE (project_id, client_id, execution_id, node_run_id, job_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id),
    CHECK (cardinality(retry_backoff_ms) = max_attempts - 1 AND 0 <= ALL (retry_backoff_ms)),
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL AND fence > 0
            AND ((current_attempt_id IS NULL) = (attempt_deadline IS NULL)))
        OR (status = 'staged' AND current_attempt_id IS NOT NULL AND fence > 0
            AND lease_owner IS NULL AND lease_until IS NULL AND attempt_deadline IS NULL)
        OR (status IN ('ready', 'done') AND current_attempt_id IS NULL AND lease_owner IS NULL
            AND lease_until IS NULL AND attempt_deadline IS NULL)
    )
);

CREATE INDEX jobs_ready_idx
    ON jimu_workflow.jobs (project_id, client_id, ready_at, execution_id, job_id)
    WHERE status = 'ready';
CREATE INDEX jobs_lease_idx
    ON jimu_workflow.jobs (project_id, client_id, lease_until, execution_id, job_id)
    WHERE status = 'leased';
CREATE INDEX executions_cancel_idx
    ON jimu_workflow.executions (project_id, client_id, cancel_requested_at, execution_id)
    WHERE cancel_requested_at IS NOT NULL AND status IN ('initialized', 'running');

CREATE TABLE jimu_workflow.attempts (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 1024),
    job_id text NOT NULL,
    fence bigint NOT NULL CHECK (fence > 0),
    lease_owner text NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, attempt_id),
    UNIQUE (project_id, client_id, execution_id, attempt_id),
    UNIQUE (project_id, client_id, execution_id, node_run_id, attempt_number),
    UNIQUE (project_id, client_id, execution_id, job_id, fence),
    UNIQUE (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id),
    UNIQUE (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id, lease_owner),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, job_id)
        REFERENCES jimu_workflow.jobs (project_id, client_id, execution_id, node_run_id, job_id)
);

ALTER TABLE jimu_workflow.jobs
    ADD CONSTRAINT job_current_attempt_fk
        FOREIGN KEY (project_id, client_id, execution_id, node_run_id, job_id, fence, current_attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id),
    ADD CONSTRAINT job_current_attempt_owner_fk
        FOREIGN KEY (project_id, client_id, execution_id, node_run_id, job_id, fence, current_attempt_id, lease_owner)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id, lease_owner);

CREATE TABLE jimu_workflow.attempt_results (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('succeeded', 'failed', 'timed_out', 'cancelled', 'superseded')),
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, attempt_id),
    UNIQUE (project_id, client_id, execution_id, node_run_id, attempt_id, status),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, attempt_id)
);

CREATE TABLE jimu_workflow.produced_outputs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    output_key text NOT NULL CHECK (output_key <> ''),
    output_kind text NOT NULL CHECK (output_kind <> ''),
    output_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, attempt_id, output_key),
    UNIQUE (project_id, client_id, execution_id, node_run_id, attempt_id, output_key, output_digest, output_kind),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, attempt_id)
);

CREATE TABLE jimu_workflow.authority_outputs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    output_key text NOT NULL,
    output_digest jimu_workflow.digest NOT NULL,
    output_kind text GENERATED ALWAYS AS ('node_output'::text) STORED,
    CHECK (output_key = 'node.output'),
    attempt_status text GENERATED ALWAYS AS ('succeeded'::text) STORED,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id, output_key, output_digest, output_kind)
        REFERENCES jimu_workflow.produced_outputs (project_id, client_id, execution_id, node_run_id, attempt_id, output_key, output_digest, output_kind),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id, attempt_status)
        REFERENCES jimu_workflow.attempt_results (project_id, client_id, execution_id, node_run_id, attempt_id, status)
);

CREATE TABLE jimu_workflow.execution_events (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    event_number bigint NOT NULL CHECK (event_number > 0),
    event_id text NOT NULL,
    recorded_at timestamptz NOT NULL,
    record json NOT NULL,
    task_id text,
    node_run_id text,
    attempt_id text,
    PRIMARY KEY (project_id, client_id, execution_id, event_number),
    UNIQUE (project_id, client_id, execution_id, event_id),
    UNIQUE (project_id, client_id, execution_id, event_number, node_run_id, attempt_id),
    CHECK (attempt_id IS NULL OR node_run_id IS NOT NULL),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id),
    FOREIGN KEY (project_id, client_id, execution_id, task_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id, task_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, attempt_id)
);

CREATE TABLE jimu_workflow.successors (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    upstream_node_run_id text NOT NULL,
    downstream_node_run_id text NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, upstream_node_run_id, downstream_node_run_id),
    CHECK (upstream_node_run_id <> downstream_node_run_id),
    FOREIGN KEY (project_id, client_id, execution_id, upstream_node_run_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id),
    FOREIGN KEY (project_id, client_id, execution_id, downstream_node_run_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id)
);

CREATE TABLE jimu_workflow.job_completions (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    job_id text NOT NULL,
    fence bigint NOT NULL,
    attempt_id text NOT NULL,
    result_digest jimu_workflow.digest NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, job_id, fence),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id)
        REFERENCES jimu_workflow.attempt_results (project_id, client_id, execution_id, node_run_id, attempt_id)
);

CREATE TABLE jimu_workflow.late_responses (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    response_key text NOT NULL,
    result_digest jimu_workflow.digest NOT NULL,
    external_request_id text,
    response_ref json NOT NULL,
    event_number bigint NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, attempt_id, response_key),
    UNIQUE (project_id, client_id, execution_id, event_number),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, attempt_id)
        REFERENCES jimu_workflow.attempt_results (project_id, client_id, execution_id, node_run_id, attempt_id),
    FOREIGN KEY (project_id, client_id, execution_id, event_number, node_run_id, attempt_id)
        REFERENCES jimu_workflow.execution_events (project_id, client_id, execution_id, event_number, node_run_id, attempt_id)
);

CREATE TABLE jimu_workflow.engine_outputs (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    output_key text NOT NULL CHECK (output_key <> ''),
    output_kind text NOT NULL CHECK (output_kind <> ''),
    output_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, output_key),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id)
        REFERENCES jimu_workflow.node_runs (project_id, client_id, execution_id, node_run_id)
);

CREATE TABLE jimu_workflow.decision_drafts (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    node_run_id text NOT NULL,
    attempt_id text NOT NULL,
    job_id text NOT NULL,
    fence bigint NOT NULL,
    draft_key text NOT NULL,
    draft_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, node_run_id, attempt_id),
    UNIQUE (project_id, client_id, execution_id, node_run_id, draft_key),
    FOREIGN KEY (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id)
        REFERENCES jimu_workflow.attempts (project_id, client_id, execution_id, node_run_id, job_id, fence, attempt_id)
);

CREATE TABLE jimu_workflow.execution_finalizations (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    finalization_digest jimu_workflow.digest NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id)
);

GRANT SELECT, INSERT ON
    jimu_workflow.intake_intents, jimu_workflow.tasks, jimu_workflow.executions,
    jimu_workflow.node_runs, jimu_workflow.jobs, jimu_workflow.attempts,
    jimu_workflow.attempt_results, jimu_workflow.produced_outputs,
    jimu_workflow.authority_outputs, jimu_workflow.execution_events,
    jimu_workflow.successors, jimu_workflow.job_completions,
    jimu_workflow.late_responses, jimu_workflow.engine_outputs,
    jimu_workflow.decision_drafts, jimu_workflow.execution_finalizations
    TO jimu_workflow_app;
GRANT UPDATE (status, failure_code, task_id, execution_id) ON jimu_workflow.intake_intents TO jimu_workflow_app;
GRANT UPDATE (status) ON jimu_workflow.tasks TO jimu_workflow_app;
GRANT UPDATE (state, status, next_event_number, cancel_requested_at) ON jimu_workflow.executions TO jimu_workflow_app;
GRANT UPDATE (state, status) ON jimu_workflow.node_runs TO jimu_workflow_app;
GRANT UPDATE (status, fence, lease_owner, lease_until, ready_at, attempt_count, current_attempt_id, attempt_deadline)
    ON jimu_workflow.jobs TO jimu_workflow_app;
