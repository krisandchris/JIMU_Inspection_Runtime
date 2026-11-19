-- Only a receipt observed inside its original, live nested model fences is qualified.
-- Existing records have no qualification witness.
ALTER TABLE jimu_workflow.dispatch_results
    ADD COLUMN fence_qualified boolean,
    ADD CONSTRAINT qualified_dispatch_receipt CHECK (
        fence_qualified IS DISTINCT FROM true OR (NOT late AND boundary <> 'not_dispatched')
    );

CREATE INDEX dispatch_nested_model_budget_idx ON jimu_workflow.dispatch_intents
    (project_id, client_id, execution_id, operation_node_run_id, operation_id)
    WHERE send_admitted AND record -> 'target' ->> 'kind' = 'nested_model';
