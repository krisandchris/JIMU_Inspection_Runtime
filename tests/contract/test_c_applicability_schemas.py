import json

import pytest
from fixtures.wp01c.applicability.cases import wire_case
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityLevelTrace,
    ApplicabilityTrace,
    ProfileEligibilityEvaluation,
    RuleApplicabilityEvaluation,
    RuleSetApplicabilityEvaluation,
    UnresolvedObligation,
)


def entries():
    _, records = wire_case("H01")
    _, reached = wire_case("H05", object_scope=True)
    return [
        ("profile_gate", ProfileEligibilityEvaluation, records["profile_eligibility"]),
        ("trace", ApplicabilityTrace, records["traces"][2]),
        ("evaluated_level", ApplicabilityLevelTrace, reached["traces"][2]["levels"][2]),
        ("projected_level", ApplicabilityLevelTrace, records["traces"][2]["levels"][2]),
        (
            "projected_rule_set",
            RuleSetApplicabilityEvaluation,
            records["rule_set_evaluations"][0],
        ),
        ("projected_rule", RuleApplicabilityEvaluation, records["rule_evaluations"][0]),
        (
            "evaluated_rule_set",
            RuleSetApplicabilityEvaluation,
            reached["rule_set_evaluations"][0],
        ),
        ("evaluated_rule", RuleApplicabilityEvaluation, reached["rule_evaluations"][0]),
        (
            "direct_obligation",
            UnresolvedObligation,
            records["unresolved_obligations"][0],
        ),
        (
            "projected_obligation",
            UnresolvedObligation,
            records["unresolved_obligations"][2],
        ),
    ]


@pytest.mark.parametrize(
    ("name", "model", "raw"), entries(), ids=[entry[0] for entry in entries()]
)
def test_applicability_wire_roundtrip_and_strict_schema(name, model, raw):
    adapter = TypeAdapter(model)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(raw)
    parsed = adapter.validate_json(json.dumps(raw))
    assert parsed.model_dump(mode="json") == raw
    assert adapter.validate_python(parsed) == parsed

    bad = raw | {"unexpected_field": "invalid"}
    assert not validator.is_valid(bad)
    with pytest.raises(ValidationError, match="unexpected_field"):
        adapter.validate_json(json.dumps(bad))
    if "schema_version" in raw:
        unversioned = {
            key: value for key, value in raw.items() if key != "schema_version"
        }
        assert not validator.is_valid(unversioned)
        with pytest.raises(ValidationError, match="schema_version"):
            adapter.validate_json(json.dumps(unversioned))
