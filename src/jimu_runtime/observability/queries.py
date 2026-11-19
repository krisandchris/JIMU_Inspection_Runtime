"""Closed PostgreSQL projections. Every query filters the composite owner key."""

SCOPE = """
    WITH owner AS (
        SELECT %s::text AS project_id, %s::text AS client_id
    )
"""
EXECUTION = """
    SELECT e.task_id, e.execution_id, e.record #>> '{trace,trace_id}' AS trace_id,
           t.status AS task_status, e.status AS execution_status,
           (t.record ->> 'accepted_at')::timestamptz AS accepted_at,
           (e.record ->> 'created_at')::timestamptz AS initialized_at,
           (e.state ->> 'started_at')::timestamptz AS started_at,
           (e.state ->> 'ended_at')::timestamptz AS ended_at,
           e.next_event_number - 1 AS through
    FROM jimu_workflow.executions e
    JOIN jimu_workflow.tasks t USING (project_id, client_id, task_id)
    WHERE e.project_id = %s AND e.client_id = %s AND e.execution_id = %s
"""
EVENTS = """
    SELECT v.event_number AS number, v.recorded_at,
           v.record #>> '{payload,event_kind}' AS kind,
           v.record #>> '{payload,next_status}' AS status,
           v.record #>> '{trace,span_id}' AS span_id,
           v.record #>> '{trace,parent_span_id}' AS parent_span_id,
           v.node_run_id, n.node_id, v.attempt_id, a.attempt_number, d.dispatch_id
    FROM jimu_workflow.execution_events v
    LEFT JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
    LEFT JOIN jimu_workflow.attempts a USING (project_id, client_id, execution_id, node_run_id, attempt_id)
    LEFT JOIN jimu_workflow.late_responses l ON
        (l.project_id, l.client_id, l.execution_id, l.event_number, l.node_run_id, l.attempt_id)
        = (v.project_id, v.client_id, v.execution_id, v.event_number, v.node_run_id, v.attempt_id)
        AND v.record #>> '{payload,event_kind}' = 'late_response'
        AND l.response_ref::jsonb = (v.record #> '{payload,response_ref}')::jsonb
    LEFT JOIN jimu_workflow.produced_outputs b
        ON (b.project_id, b.client_id, b.execution_id, b.node_run_id, b.attempt_id)
           = (l.project_id, l.client_id, l.execution_id, l.node_run_id, l.attempt_id)
        AND b.output_key = 'late.artifact.' || l.response_key
        AND b.output_kind = 'late_artifact'
        AND l.response_ref ->> 'record_kind' = 'observation_batch'
        AND b.record ->> 'observation_batch_id' = l.response_ref ->> 'record_id'
        AND b.record ->> 'execution_id' = v.execution_id
        AND b.record #>> '{attempt_ref,record_id}' = v.attempt_id
        AND b.record #>> '{node_run_ref,record_id}' = v.node_run_id
    LEFT JOIN jimu_workflow.dispatch_intents d
        ON (d.project_id, d.client_id, d.execution_id, d.node_run_id, d.attempt_id)
           = (v.project_id, v.client_id, v.execution_id, v.node_run_id, v.attempt_id)
        AND d.dispatch_id = CASE l.response_ref ->> 'record_kind'
            WHEN 'model_invocation' THEN l.response_ref ->> 'record_id'
            WHEN 'tool_execution' THEN l.response_ref ->> 'record_id'
            WHEN 'observation_batch' THEN b.record #>> '{model_invocation_ref,record_id}'
        END
        AND EXISTS (
            SELECT 1 FROM jimu_workflow.dispatch_results r
            WHERE (r.project_id, r.client_id, r.execution_id, r.dispatch_id)
                  = (d.project_id, d.client_id, d.execution_id, d.dispatch_id)
              AND CASE l.response_ref ->> 'record_kind'
                WHEN 'tool_execution' THEN r.record #>> '{execution,tool_execution_id}'
                ELSE r.record #>> '{invocation,model_invocation_id}'
              END = d.dispatch_id
        )
    WHERE v.project_id = %s AND v.client_id = %s AND v.execution_id = %s
      AND v.event_number > %s AND v.event_number <= %s
    ORDER BY v.event_number
    LIMIT %s
"""
CALLS = """
    WITH selected AS (
        SELECT project_id, client_id, execution_id, dispatch_id, recorded_at,
               node_run_id, attempt_id, operation_node_run_id, send_admitted,
               record #>> '{target,kind}' AS target_kind
        FROM jimu_workflow.dispatch_intents
        WHERE project_id = %s AND client_id = %s AND execution_id = %s
          AND (%s::text IS NULL OR attempt_id = %s)
          AND (%s::timestamptz IS NULL OR
              (recorded_at, dispatch_id COLLATE "C") >
              (%s::timestamptz, %s::text COLLATE "C"))
        ORDER BY recorded_at, dispatch_id COLLATE "C"
        LIMIT %s
    )
    SELECT i.dispatch_id, i.recorded_at, i.node_run_id, n.node_id, i.attempt_id,
           i.operation_node_run_id, s.parent_node_run_id, s.parent_attempt_id,
           i.send_admitted, i.target_kind,
           r.recorded_at AS receipt_at, r.boundary, r.late, r.fence_qualified,
           r.record #>> '{error,error_class}' AS error_class,
           COALESCE(r.record #>> '{invocation,outcome,status}', r.record #>> '{execution,status}') AS outcome,
           COALESCE(r.record #>> '{invocation,model_invocation_id}', r.record #>> '{execution,tool_execution_id}') AS producer_id,
           COALESCE(r.record #>> '{invocation,started_at}', r.record #>> '{execution,started_at}')::timestamptz AS started_at,
           COALESCE(r.record #>> '{invocation,completed_at}', r.record #>> '{execution,completed_at}')::timestamptz AS completed_at,
           (r.record ->> 'response_evidence_error') IS NOT NULL AS response_evidence_failed,
           COALESCE(json_array_length(r.record -> 'invalid_question_slots'), 0) AS invalid_slots,
           COALESCE(json_array_length(r.record -> 'missing_question_slots'), 0) AS missing_slots
    FROM selected i
    JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
    LEFT JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
    LEFT JOIN jimu_workflow.tool_step_attempts s ON
        (i.project_id, i.client_id, i.execution_id, i.node_run_id, i.attempt_id)
        = (s.project_id, s.client_id, s.execution_id, s.child_node_run_id, s.child_attempt_id)
    ORDER BY i.recorded_at, i.dispatch_id COLLATE "C", r.late
"""
TOOL_STEPS = """
    SELECT s.step_id, s.parent_node_run_id, s.parent_attempt_id,
           s.child_node_run_id, s.child_attempt_id, c.tool_execution_id,
           COALESCE(r.record ->> 'started_at', a.record ->> 'started_at')::timestamptz AS started_at,
           (r.record ->> 'ended_at')::timestamptz AS ended_at,
           COALESCE(r.status, a.record ->> 'status') AS attempt_status,
           c.record #>> '{result,execution,status}' AS outcome
    FROM jimu_workflow.tool_step_attempts s
    JOIN jimu_workflow.attempts a ON
        (s.project_id, s.client_id, s.execution_id, s.child_node_run_id, s.child_attempt_id)
        = (a.project_id, a.client_id, a.execution_id, a.node_run_id, a.attempt_id)
    LEFT JOIN jimu_workflow.attempt_results r ON
        (s.project_id, s.client_id, s.execution_id, s.child_node_run_id, s.child_attempt_id)
        = (r.project_id, r.client_id, r.execution_id, r.node_run_id, r.attempt_id)
    LEFT JOIN jimu_workflow.tool_step_completions c ON
        (s.project_id, s.client_id, s.execution_id, s.child_node_run_id, s.child_attempt_id)
        = (c.project_id, c.client_id, c.execution_id, c.child_node_run_id, c.child_attempt_id)
    WHERE s.project_id = %s AND s.client_id = %s AND s.execution_id = %s
      AND (%s::text IS NULL OR s.child_attempt_id COLLATE "C" > %s::text COLLATE "C")
    ORDER BY s.child_attempt_id COLLATE "C"
    LIMIT %s
"""

# bool/enum projection prevents novel journal error codes and IDs becoming labels.
# Each receipt is counted for audit, while usage/duration uses one
# attributable physical result per dispatch (normal preferred to late).
OBSERVATIONS = """
    , receipts AS (
        SELECT i.execution_id, i.dispatch_id, r.late, r.boundary,
               CASE WHEN i.record #>> '{target,kind}' = 'tool' THEN 'tool'
                    WHEN i.record #>> '{target,kind}' IN
                        ('model','model_unselected','nested_model','nested_model_unselected') THEN 'model'
                    ELSE 'unknown' END AS kind,
               COALESCE(r.record #>> '{invocation,outcome,status}', r.record #>> '{execution,status}') AS outcome,
               COALESCE(r.record #>> '{invocation,started_at}', r.record #>> '{execution,started_at}')::timestamptz AS started_at,
               COALESCE(r.record #>> '{invocation,completed_at}', r.record #>> '{execution,completed_at}')::timestamptz AS completed_at,
               (r.record #>> '{invocation,usage,input_tokens}')::bigint AS input_tokens,
               (r.record #>> '{invocation,usage,output_tokens}')::bigint AS output_tokens,
               (r.record ->> 'response_evidence_error') IS NOT NULL AS evidence_error,
               r.record #>> '{error,error_class}' AS error_class,
               COALESCE(json_array_length(r.record -> 'invalid_question_slots'), 0) AS invalid_slots,
               COALESCE(json_array_length(r.record -> 'missing_question_slots'), 0) AS missing_slots
        FROM owner
        JOIN jimu_workflow.dispatch_intents i USING (project_id, client_id)
        JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
    )
"""
MEASUREMENTS = """
    , elapsed_results AS (
        SELECT DISTINCT ON (execution_id, dispatch_id) * FROM receipts
        WHERE boundary <> 'not_dispatched' AND completed_at >= started_at
        ORDER BY execution_id, dispatch_id, late
    ), input_reports AS (
        SELECT DISTINCT ON (execution_id, dispatch_id) input_tokens FROM receipts
        WHERE boundary <> 'not_dispatched' AND input_tokens IS NOT NULL
        ORDER BY execution_id, dispatch_id, late
    ), output_reports AS (
        SELECT DISTINCT ON (execution_id, dispatch_id) output_tokens FROM receipts
        WHERE boundary <> 'not_dispatched' AND output_tokens IS NOT NULL
        ORDER BY execution_id, dispatch_id, late
    )
"""
COUNTS = (
    SCOPE
    + OBSERVATIONS
    + MEASUREMENTS
    + """
    SELECT 'tasks' AS family, status AS first_label, '' AS second_label, count(*)::numeric AS value
    FROM owner JOIN jimu_workflow.tasks USING (project_id, client_id) GROUP BY status
    UNION ALL
    SELECT 'accepted_tasks', '', '', count(*)::numeric
    FROM owner JOIN jimu_workflow.tasks USING (project_id, client_id)
    UNION ALL
    SELECT 'executions', status, '', count(*)::numeric
    FROM owner JOIN jimu_workflow.executions USING (project_id, client_id) GROUP BY status
    UNION ALL
    SELECT 'jobs', j.status, '', count(*)::numeric
    FROM owner JOIN jimu_workflow.jobs j USING (project_id, client_id)
    WHERE NOT internal_tool_step GROUP BY j.status
    UNION ALL
    SELECT 'nodes', n.node_id, n.status, count(*)::numeric
    FROM owner JOIN jimu_workflow.node_runs n USING (project_id, client_id)
    LEFT JOIN jimu_workflow.jobs j USING (project_id, client_id, execution_id, node_run_id)
    WHERE j.internal_tool_step IS DISTINCT FROM TRUE GROUP BY n.node_id, n.status
    UNION ALL
    SELECT 'retries', n.node_id, '', count(*)::numeric
    FROM owner JOIN jimu_workflow.attempts a USING (project_id, client_id)
    JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
    JOIN jimu_workflow.jobs j USING (project_id, client_id, execution_id, node_run_id, job_id)
    WHERE a.attempt_number > 1 AND NOT j.internal_tool_step GROUP BY n.node_id
    UNION ALL
    SELECT 'attempt_results', n.node_id, r.status, count(*)::numeric
    FROM owner JOIN jimu_workflow.attempt_results r USING (project_id, client_id)
    JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
    JOIN jimu_workflow.jobs j USING (project_id, client_id, execution_id, node_run_id)
    WHERE NOT j.internal_tool_step GROUP BY n.node_id, r.status
    UNION ALL
    SELECT 'physical_dispatches',
           CASE WHEN record #>> '{target,kind}' = 'tool' THEN 'tool'
                WHEN record #>> '{target,kind}' IN
                    ('model','model_unselected','nested_model','nested_model_unselected') THEN 'model'
                ELSE 'unknown' END, '', count(*)::numeric
    FROM owner JOIN jimu_workflow.dispatch_intents USING (project_id, client_id)
    WHERE send_admitted GROUP BY 2
    UNION ALL
    SELECT 'receipts', kind, boundary, count(*)::numeric FROM receipts GROUP BY kind, boundary
    UNION ALL
    SELECT 'call_outcomes', kind,
           CASE WHEN outcome IN ('succeeded','partial','failed','not_dispatched') THEN outcome
                ELSE 'unqualified' END, count(*)::numeric
    FROM receipts GROUP BY 2, 3
    UNION ALL
    SELECT 'call_errors', kind,
           CASE WHEN error_class IN ('transport','timeout','rate_limit','unavailable',
                       'validation','association','resource_exhausted','cancelled','internal')
                THEN error_class ELSE 'other' END, count(*)::numeric
    FROM receipts WHERE error_class IS NOT NULL GROUP BY 2, 3
    UNION ALL
    SELECT 'late_receipts', kind, '', count(*)::numeric FROM receipts WHERE late GROUP BY kind
    UNION ALL
    SELECT 'response_evidence_failures', '', '', count(*)::numeric FROM receipts WHERE evidence_error
    UNION ALL
    SELECT 'observation_slots', 'invalid', '', sum(invalid_slots)::numeric FROM receipts HAVING sum(invalid_slots) > 0
    UNION ALL
    SELECT 'observation_slots', 'missing', '', sum(missing_slots)::numeric FROM receipts HAVING sum(missing_slots) > 0
    UNION ALL
    SELECT 'token_values', 'input', '', sum(input_tokens)::numeric FROM input_reports HAVING count(input_tokens) > 0
    UNION ALL
    SELECT 'token_values', 'output', '', sum(output_tokens)::numeric FROM output_reports HAVING count(output_tokens) > 0
    UNION ALL
    SELECT 'token_observations', 'input', '', count(*)::numeric FROM input_reports
    UNION ALL
    SELECT 'token_observations', 'output', '', count(*)::numeric FROM output_reports
    UNION ALL
    SELECT 'tool_steps', attempt_status, '', count(*)::numeric
    FROM owner JOIN jimu_workflow.tool_step_completions USING (project_id, client_id) GROUP BY attempt_status
"""
)

# Task latency follows only the original intake Execution. Replay executions do
# not increase the count of accepted tasks or overwrite intake latency.
HISTOGRAMS = (
    SCOPE
    + OBSERVATIONS
    + MEASUREMENTS
    + """
    , times AS (
        SELECT 'queue' AS family, '' AS label,
               (e.state ->> 'started_at')::timestamptz AS end_at,
               (t.record ->> 'accepted_at')::timestamptz AS start_at
        FROM owner JOIN jimu_workflow.intake_intents i USING (project_id, client_id)
        JOIN jimu_workflow.tasks t USING (project_id, client_id, task_id)
        JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id, task_id)
        WHERE i.status = 'accepted'
        UNION ALL
        SELECT 'task', '', (e.state ->> 'ended_at')::timestamptz,
               (t.record ->> 'accepted_at')::timestamptz
        FROM owner JOIN jimu_workflow.intake_intents i USING (project_id, client_id)
        JOIN jimu_workflow.tasks t USING (project_id, client_id, task_id)
        JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id, task_id)
        WHERE i.status = 'accepted'
        UNION ALL
        SELECT 'node', n.node_id, (n.state ->> 'ended_at')::timestamptz, (n.state ->> 'started_at')::timestamptz
        FROM owner JOIN jimu_workflow.node_runs n USING (project_id, client_id)
        LEFT JOIN jimu_workflow.jobs j USING (project_id, client_id, execution_id, node_run_id)
        WHERE j.internal_tool_step IS DISTINCT FROM TRUE
        UNION ALL
        SELECT kind, '', completed_at, started_at FROM elapsed_results WHERE kind IN ('model','tool')
    ), seconds AS (
        SELECT family, label, EXTRACT(epoch FROM (end_at - start_at)) AS duration
        FROM times WHERE end_at >= start_at
    )
    SELECT family, label, count(*) AS samples, sum(duration) AS total,
           ARRAY[count(*) FILTER (WHERE duration <= 0.05),
                 count(*) FILTER (WHERE duration <= 0.1),
                 count(*) FILTER (WHERE duration <= 0.25),
                 count(*) FILTER (WHERE duration <= 0.5),
                 count(*) FILTER (WHERE duration <= 1),
                 count(*) FILTER (WHERE duration <= 2),
                 count(*) FILTER (WHERE duration <= 5),
                 count(*) FILTER (WHERE duration <= 10),
                 count(*) FILTER (WHERE duration <= 30),
                 count(*) FILTER (WHERE duration <= 120)] AS buckets
    FROM seconds GROUP BY family, label
"""
)

# Source tables in these revisions are append-only. Row transaction identities
# also detect in-place changes and replacement at the same count on this database.
CALL_REVISION = """
    WITH selected AS (
        SELECT i.project_id, i.client_id, i.execution_id, i.dispatch_id,
               i.node_run_id, i.attempt_id, i.xmin::text::numeric AS row_version
        FROM jimu_workflow.dispatch_intents i
        WHERE i.project_id = %s AND i.client_id = %s AND i.execution_id = %s
          AND (%s::text IS NULL OR i.attempt_id = %s)
    )
    SELECT (SELECT count(*) FROM selected) AS intents,
           (SELECT sum(row_version) FROM selected) AS intent_version,
           count(r.dispatch_id) AS receipts, sum(r.xmin::text::numeric) AS receipt_version,
           count(s.child_attempt_id) AS lineages, sum(s.xmin::text::numeric) AS lineage_version
    FROM selected i
    LEFT JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
    LEFT JOIN jimu_workflow.tool_step_attempts s ON
        (i.project_id,i.client_id,i.execution_id,i.node_run_id,i.attempt_id)
        = (s.project_id,s.client_id,s.execution_id,s.child_node_run_id,s.child_attempt_id)
"""
TOOL_REVISION = """
    SELECT count(*) AS steps, sum(s.xmin::text::numeric) AS step_version,
           count(r.attempt_id) AS results, sum(r.xmin::text::numeric) AS result_version,
           count(c.child_attempt_id) AS completions, sum(c.xmin::text::numeric) AS completion_version,
           sum(a.xmin::text::numeric) AS attempt_version
    FROM jimu_workflow.tool_step_attempts s
    JOIN jimu_workflow.attempts a ON
        (s.project_id,s.client_id,s.execution_id,s.child_node_run_id,s.child_attempt_id)
        = (a.project_id,a.client_id,a.execution_id,a.node_run_id,a.attempt_id)
    LEFT JOIN jimu_workflow.attempt_results r ON
        (s.project_id,s.client_id,s.execution_id,s.child_node_run_id,s.child_attempt_id)
        = (r.project_id,r.client_id,r.execution_id,r.node_run_id,r.attempt_id)
    LEFT JOIN jimu_workflow.tool_step_completions c ON
        (s.project_id,s.client_id,s.execution_id,s.child_node_run_id,s.child_attempt_id)
        = (c.project_id,c.client_id,c.execution_id,c.child_node_run_id,c.child_attempt_id)
    WHERE s.project_id = %s AND s.client_id = %s AND s.execution_id = %s
"""
