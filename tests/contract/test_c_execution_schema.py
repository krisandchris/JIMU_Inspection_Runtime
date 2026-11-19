"""C4 persisted roots validate against generated Draft 2020-12 schemas."""

import json

import pytest
from fixtures.wp01c.executions.support import fixture
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.runtime.executions import (
    Attempt,
    ExecutionEvent,
    ExecutionRecord,
    NodeRun,
)


@pytest.mark.parametrize(
    "model,example",
    [
        (ExecutionRecord, "execution"),
        (NodeRun, "node"),
        (Attempt, "attempt"),
        (ExecutionEvent, "late_event"),
        (ExecutionEvent, "system_event"),
    ],
)
def test_versioned_strict_json_roots_roundtrip_and_schema_parity(model, example):
    document = fixture()[example]
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(document)
    parsed = model.model_validate_json(json.dumps(document))
    assert model.model_validate_json(parsed.model_dump_json()) == parsed
    validator.validate(parsed.model_dump(mode="json"))

    missing_version = {
        key: val for key, val in document.items() if key != "schema_version"
    }
    for invalid in (
        missing_version,
        document | {"schema_version": "1.0.1"},
        document | {"extra": 0},
    ):
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(invalid))
