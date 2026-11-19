import json

import pytest
from fixtures.wp01c.nodes.cases import cases
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.nodes.context import ContextBuildInput, ContextBuildOutput
from jimu_runtime.domain.nodes.decision import DecisionInput, DecisionOutput
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryInput,
    DiscoveryOutput,
    DiscoveryProfileDiagnostic,
)
from jimu_runtime.domain.nodes.envelopes import (
    CancelledNodeOutput,
    FailedNodeOutput,
    NodeInputEnvelope,
    NodeOutputEnvelope,
    SucceededNodeOutput,
)
from jimu_runtime.domain.nodes.grounding import GroundingInput, GroundingOutput
from jimu_runtime.domain.nodes.observation import ObservationInput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput, ToolNodeOutput
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import GroundingRecord, ObjectRecord

ROOTS = {
    "input_envelope": NodeInputEnvelope,
    "output_envelope": NodeOutputEnvelope,
    "failed_envelope": FailedNodeOutput,
    "cancelled_envelope": CancelledNodeOutput,
    "context_input": ContextBuildInput,
    "context_output": ContextBuildOutput,
    "context_failure": ContextBuildOutput,
    "discovery_input": DiscoveryInput,
    "discovery_output": DiscoveryOutput,
    "discovery_diagnostic": DiscoveryProfileDiagnostic,
    "skipped_discovery": DiscoveryOutput,
    "grounding_input": GroundingInput,
    "grounding_output": GroundingOutput,
    "grounding_record": GroundingRecord,
    "object_record": ObjectRecord,
    "observation_input": ObservationInput,
    "batch": ObservationBatch,
    "tool_input": ToolNodeInput,
    "tool_output": ToolNodeOutput,
    "decision_input": DecisionInput,
    "decision_output": DecisionOutput,
}


@pytest.mark.parametrize("case", ROOTS)
def test_node_root_schema_and_wire(case):
    raw = cases()[case]
    adapter = TypeAdapter(ROOTS[case])
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(raw)
    parsed = adapter.validate_json(json.dumps(raw))
    validator.validate(parsed.model_dump(mode="json"))
    assert adapter.validate_json(parsed.model_dump_json()) == parsed
    assert adapter.validate_python(parsed) == parsed

    unversioned = {key: value for key, value in raw.items() if key != "schema_version"}
    for invalid in (unversioned, raw | {"unchecked_extension": True}):
        assert not validator.is_valid(invalid)
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps(invalid))


def test_direct_succeeded_branch_has_an_independent_wire_roundtrip():
    adapter = TypeAdapter(SucceededNodeOutput)
    raw = cases()["output_envelope"]
    Draft202012Validator(adapter.json_schema()).validate(raw)
    assert adapter.validate_json(json.dumps(raw)).model_dump(mode="json") == raw
