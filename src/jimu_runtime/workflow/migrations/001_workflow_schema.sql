CREATE SCHEMA jimu_workflow;
REVOKE ALL ON SCHEMA jimu_workflow FROM PUBLIC;
GRANT USAGE ON SCHEMA jimu_workflow TO jimu_workflow_app;
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE jimu_workflow.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    script_name text NOT NULL UNIQUE,
    script_sha256 text NOT NULL CHECK (script_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON TABLE jimu_workflow.schema_migrations FROM PUBLIC, jimu_workflow_app;
