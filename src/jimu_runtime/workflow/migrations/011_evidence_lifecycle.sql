-- Holds and disposal are additional records. Historical producers retain their bytes' address and digest.
CREATE INDEX evidence_output_file_idx ON jimu_workflow.produced_outputs
    ((record -> 'storage' ->> 'store_id'), (record -> 'storage' ->> 'object_key'))
    WHERE output_kind = 'evidence';

CREATE TABLE jimu_workflow.evidence_retention_holds (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    hold_id text NOT NULL CHECK (hold_id <> ''),
    store_id text NOT NULL CHECK (store_id <> ''),
    file_key text NOT NULL CHECK (file_key ~ '^[0-9a-f]{64}$'),
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, hold_id),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record ->> 'hold_id') IS NOT DISTINCT FROM hold_id),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id)
);
CREATE INDEX evidence_retention_hold_file_idx ON jimu_workflow.evidence_retention_holds (store_id, file_key);

CREATE TABLE jimu_workflow.evidence_retention_hold_releases (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    hold_id text NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (project_id, client_id, execution_id, hold_id),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record ->> 'hold_id') IS NOT DISTINCT FROM hold_id),
    CHECK (record ->> 'released_at' IS NOT NULL),
    FOREIGN KEY (project_id, client_id, execution_id, hold_id)
        REFERENCES jimu_workflow.evidence_retention_holds
            (project_id, client_id, execution_id, hold_id)
);

CREATE TABLE jimu_workflow.evidence_disposition_intents (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    store_id text NOT NULL CHECK (store_id <> ''),
    file_key text NOT NULL CHECK (file_key ~ '^[0-9a-f]{64}$'),
    record json NOT NULL,
    PRIMARY KEY (store_id, file_key),
    UNIQUE (project_id, client_id, execution_id, store_id, file_key),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record -> 'storage' ->> 'object_key') IS NOT DISTINCT FROM ('media-v1/' || file_key)),
    CHECK ((record -> 'storage' ->> 'store_id') IS NOT DISTINCT FROM store_id),
    FOREIGN KEY (project_id, client_id, execution_id)
        REFERENCES jimu_workflow.executions (project_id, client_id, execution_id)
);

CREATE TABLE jimu_workflow.evidence_disposition_completions (
    project_id text NOT NULL,
    client_id text NOT NULL,
    execution_id text NOT NULL,
    store_id text NOT NULL,
    file_key text NOT NULL,
    record json NOT NULL,
    PRIMARY KEY (store_id, file_key),
    CHECK ((record ->> 'schema_version') IS NOT DISTINCT FROM '1.0.0'),
    CHECK ((record ->> 'outcome') IS NOT NULL AND
        (record ->> 'outcome') IN ('removed', 'absent_on_recovery')),
    CHECK ((record ->> 'completed_at') IS NOT NULL),
    FOREIGN KEY (project_id, client_id, execution_id, store_id, file_key)
        REFERENCES jimu_workflow.evidence_disposition_intents
            (project_id, client_id, execution_id, store_id, file_key)
);

GRANT SELECT, INSERT ON
    jimu_workflow.evidence_retention_holds,
    jimu_workflow.evidence_retention_hold_releases,
    jimu_workflow.evidence_disposition_intents,
    jimu_workflow.evidence_disposition_completions
    TO jimu_workflow_app;


-- The durable decision must cover each reference's elapsed deadline.
CREATE FUNCTION jimu_workflow.valid_disposition_cutoff(body json) RETURNS boolean
LANGUAGE plpgsql SET search_path = pg_catalog AS $disposition_cutoff$
DECLARE
    requested timestamptz;
BEGIN
    IF body ->> 'requested_at' IS NULL OR json_typeof(body -> 'references') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF json_array_length(body -> 'references') NOT BETWEEN 1 AND 1024 THEN
        RETURN false;
    END IF;
    requested := (body ->> 'requested_at')::timestamptz;
    RETURN isfinite(requested) AND NOT EXISTS (
        SELECT 1 FROM json_array_elements(body -> 'references') AS ref(value)
        WHERE ref.value ->> 'expires_at' IS NULL
            OR NOT isfinite((ref.value ->> 'expires_at')::timestamptz)
            OR (ref.value ->> 'expires_at')::timestamptz > requested
    );
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
    RETURN false;
END;
$disposition_cutoff$;
ALTER TABLE jimu_workflow.evidence_disposition_intents
    ADD CONSTRAINT disposition_cutoff_elapsed CHECK (jimu_workflow.valid_disposition_cutoff(record));

-- Match the signed high 64 bits of the media adapter's advisory lock key.
-- READ COMMITTED is required for these writes to look past transactions that
-- completed while this statement was waiting for that per-file lock.
CREATE FUNCTION jimu_workflow.lock_evidence_file(key text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog AS $evidence_lock$
BEGIN
    IF current_setting('transaction_isolation') NOT IN ('read committed', 'read uncommitted') THEN
        RAISE EXCEPTION 'evidence publication requires fresh transaction visibility' USING ERRCODE = '25001';
    END IF;
    PERFORM pg_advisory_xact_lock((('x' || left(key, 16))::bit(64))::bigint);
END;
$evidence_lock$;

CREATE FUNCTION jimu_workflow.guard_evidence_file_reference() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $evidence_ref$
DECLARE
    physical_path text;
    physical_store text;
    physical_key text;
BEGIN
    IF NEW.output_kind <> 'evidence' THEN
        RETURN NEW;
    END IF;
    physical_path := NEW.record -> 'storage' ->> 'object_key';
    physical_store := NEW.record -> 'storage' ->> 'store_id';
    IF physical_path IS NULL OR physical_path !~ '^media-v1/[0-9a-f]{64}$' OR physical_store IS NULL THEN
        RETURN NEW;
    END IF;
    physical_key := split_part(physical_path, '/', 2);
    PERFORM jimu_workflow.lock_evidence_file(physical_key);
    -- A byte-identical primary-key retry creates no new reference.
    IF EXISTS (
        SELECT 1 FROM jimu_workflow.produced_outputs p
        WHERE (p.project_id, p.client_id, p.execution_id, p.node_run_id, p.attempt_id, p.output_key)
            = (NEW.project_id, NEW.client_id, NEW.execution_id, NEW.node_run_id, NEW.attempt_id, NEW.output_key)
          AND (p.output_kind, p.output_digest, p.record::text)
            = (NEW.output_kind, NEW.output_digest, NEW.record::text)
    ) THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT 1 FROM jimu_workflow.evidence_disposition_intents i
        WHERE (i.store_id, i.file_key) = (physical_store, physical_key)
    ) OR EXISTS (
        SELECT 1 FROM jimu_workflow.observation_request_media_reclaims r
            JOIN jimu_workflow.observation_request_media req USING (file_key)
        WHERE (req.store_id, r.file_key) = (physical_store, physical_key)
    ) THEN
        RAISE EXCEPTION 'evidence file retired' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$evidence_ref$;
CREATE TRIGGER guard_evidence_file_reference
    BEFORE INSERT ON jimu_workflow.produced_outputs
    FOR EACH ROW EXECUTE FUNCTION jimu_workflow.guard_evidence_file_reference();

-- WP-05 terminal cleanup uses the same files, with a separate tombstone.
-- Preserve even differently named or differently scoped physical references.
CREATE FUNCTION jimu_workflow.guard_request_media_reclaim() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $request_reclaim$
DECLARE
    physical_store text;
BEGIN
    PERFORM jimu_workflow.lock_evidence_file(NEW.file_key);
    SELECT store_id INTO physical_store FROM jimu_workflow.observation_request_media
        WHERE file_key = NEW.file_key;
    IF EXISTS (
        SELECT 1 FROM jimu_workflow.produced_outputs p
        WHERE p.output_kind = 'evidence'
             AND (p.record -> 'storage' ->> 'store_id', p.record -> 'storage' ->> 'object_key')
                 = (physical_store, 'media-v1/' || NEW.file_key)
    ) OR EXISTS (
        SELECT 1 FROM jimu_workflow.evidence_disposition_intents i
        WHERE (i.store_id, i.file_key) = (physical_store, NEW.file_key)
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$request_reclaim$;
CREATE TRIGGER guard_request_media_reclaim
    BEFORE INSERT ON jimu_workflow.observation_request_media_reclaims
    FOR EACH ROW EXECUTE FUNCTION jimu_workflow.guard_request_media_reclaim();

CREATE FUNCTION jimu_workflow.guard_evidence_hold() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $evidence_hold$
BEGIN
    PERFORM jimu_workflow.lock_evidence_file(NEW.file_key);
    IF EXISTS (
        SELECT 1 FROM jimu_workflow.evidence_disposition_intents i
        WHERE (i.store_id, i.file_key) = (NEW.store_id, NEW.file_key)
    ) THEN
        RAISE EXCEPTION 'evidence disposition already committed' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$evidence_hold$;
CREATE TRIGGER guard_evidence_hold
    BEFORE INSERT ON jimu_workflow.evidence_retention_holds
    FOR EACH ROW EXECUTE FUNCTION jimu_workflow.guard_evidence_hold();

GRANT EXECUTE ON FUNCTION jimu_workflow.lock_evidence_file(text),
    jimu_workflow.valid_disposition_cutoff(json) TO jimu_workflow_app;
