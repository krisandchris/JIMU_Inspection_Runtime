CREATE TABLE jimu_workflow.fixture_history (
    id integer PRIMARY KEY
);
GRANT SELECT, INSERT ON TABLE jimu_workflow.fixture_history TO jimu_workflow_app;

CREATE FUNCTION jimu_workflow.fixture_id_present() RETURNS trigger
LANGUAGE plpgsql AS $fixture$
BEGIN
    IF NEW.id < 1 THEN
        RAISE EXCEPTION 'id must be positive';
    END IF;
    RETURN NEW;
END;
$fixture$;

CREATE TRIGGER fixture_positive_id
BEFORE INSERT ON jimu_workflow.fixture_history
FOR EACH ROW EXECUTE FUNCTION jimu_workflow.fixture_id_present();
