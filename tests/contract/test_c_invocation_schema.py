"""Offline Draft 2020-12 checks for the three full invocation audit families."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import ToolExecution

_FIXTURES = (
    Path(__file__).parents[1] / "fixtures" / "wp01c" / "invocations" / "contracts.json"
)
_DATA = json.loads(_FIXTURES.read_text())
_ROOTS = [
    ("models", ModelInvocation),
    ("selections", QuestionSelectionRecord),
    ("tools", ToolExecution),
]


@pytest.mark.parametrize(("family", "root"), _ROOTS)
def test_invocation_roots_round_trip_and_reject_structural_errors(family, root):
    adapter = TypeAdapter(root)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for payload in _DATA[family].values():
        parsed = adapter.validate_json(json.dumps(payload))
        validator.validate(payload)
        assert json.loads(adapter.dump_json(parsed)) == payload
        assert adapter.validate_python(parsed) == parsed
        for invalid in (
            {key: value for key, value in payload.items() if key != "schema_version"},
            {**payload, "authorization_header": "fixture-extra"},
        ):
            assert not validator.is_valid(invalid)
            with pytest.raises(ValidationError):
                adapter.validate_json(json.dumps(invalid))


@pytest.mark.parametrize(
    ("family", "item", "root", "field"),
    [
        ("models", "observation", ModelInvocation, "actual_provider"),
        ("tools", "step", ToolExecution, "actual_implementation_version"),
        ("tools", "step", ToolExecution, "object_ref"),
    ],
)
def test_required_branch_fields_have_schema_constraints(family, item, root, field):
    payload = _DATA[family][item].copy()
    payload.pop(field)
    adapter = TypeAdapter(root)
    assert not Draft202012Validator(adapter.json_schema()).is_valid(payload)
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(payload))
