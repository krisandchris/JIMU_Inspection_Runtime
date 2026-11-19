"""Draft 2020-12 contracts and legacy system output event compatibility."""

import json

from fixtures.wp01c.runtime_authority.cases import documents
from jsonschema import Draft202012Validator

from jimu_runtime.domain.runtime.executions import ExecutionEvent
from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords


def test_runtime_authority_sources_are_strict_generated_data():
    _, actual = documents()
    schema = RuntimeAuthorityRecords.model_json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(actual)
    assert not validator.is_valid(actual | {"unreviewed": True})
    assert not validator.is_valid(
        {key: value for key, value in actual.items() if key != "release"}
    )


def test_existing_system_event_remains_readable_without_authority_witness():
    _, actual = documents("system")
    event = actual["execution_events"][0]
    event["payload"].pop("authority_output")
    parsed = ExecutionEvent.model_validate_json(json.dumps(event))
    assert parsed.payload.authority_output is None
    Draft202012Validator(ExecutionEvent.model_json_schema()).validate(event)
