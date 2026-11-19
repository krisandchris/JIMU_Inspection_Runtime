import json

import pytest
from fixtures.wp01c.decisions.cases import supplied_case
from fixtures.wp01c.nodes.cases import PIN, decision_case, parse, root
from pydantic import ValidationError

from jimu_runtime.domain.nodes.decision import DecisionOutput
from jimu_runtime.domain.runtime.applicability import ProfileEligibilityEvaluation
from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
from jimu_runtime.workflow.engine import DecisionDraft, ExecutionFinalization


def draft_case():
    wire = decision_case()
    wire["output"]["envelope"]["output_artifacts"] = [
        {"ref": wire["output"]["decision_ref"], "digest": PIN},
    ]
    draft = root(
        execution_id=wire["decision"]["execution_id"],
        inputs=wire["input"],
        output_envelope=wire["output"]["envelope"],
        decision=wire["decision"],
        candidate=wire["candidate"],
        rule_set_evaluations=[wire["rule_set"]],
        rule_evaluations=[wire["rule"]],
    )
    return wire, draft


def test_persisted_draft_round_trips_before_shared_coverage_binding():
    wire, raw = draft_case()
    draft = parse(DecisionDraft, raw)
    parsed = DecisionDraft.model_validate_json(draft.model_dump_json())
    assert parsed == draft

    coverage = parse(ExecutionCoverage, wire["coverage"])
    output = parsed.bind_coverage(
        coverage,
        parse(ProfileEligibilityEvaluation, wire["gate"]),
        parsed.inputs.mandatory_rule_paths,
    )
    assert output == parse(DecisionOutput, wire["output"])
    finalization = ExecutionFinalization(
        schema_version="1.0.0",
        execution_id=parsed.execution_id,
        coverage=coverage,
        decisions=(parsed.decision,),
    )
    assert (
        ExecutionFinalization.model_validate_json(finalization.model_dump_json())
        == finalization
    )


@pytest.mark.parametrize("failure", ["scope", "extra", "output", "schema", "candidate"])
def test_invalid_or_unpinned_draft_rejected(failure):
    _, raw = draft_case()
    if failure == "scope":
        raw["decision"]["bundle_digest"] = "a" * 64
        raw["inputs"]["envelope"]["bundle_digest"] = "b" * 64
    elif failure == "extra":
        raw["extra"] = {}
    elif failure == "output":
        raw["output_envelope"]["output_artifacts"] = []
    elif failure == "candidate":
        raw["candidate"]["object_ref"]["record_id"] = "another-object"
    else:
        raw.pop("schema_version")
    with pytest.raises(ValidationError):
        DecisionDraft.model_validate_json(json.dumps(raw))


def test_finalization_round_trips_selected_ancestor_projections():
    coverage, records = supplied_case(
        hierarchy="H02", discovery="not_evaluated", grounding="not_evaluated"
    )
    applicability = records["applicability"]["records"]
    result = parse(
        ExecutionFinalization,
        root(
            execution_id=coverage["execution_id"],
            coverage=coverage,
            decisions=[],
            projected_traces=applicability["traces"][1:],
            projected_rule_set_evaluations=applicability["rule_set_evaluations"],
            projected_rule_evaluations=applicability["rule_evaluations"],
        ),
    )
    assert result.projected_rule_evaluations
    assert result.projected_rule_set_evaluations
    assert result.projected_traces
    assert ExecutionFinalization.model_validate_json(result.model_dump_json()) == result

    omitted = result.model_dump(mode="json")
    omitted["projected_rule_evaluations"] = []
    with pytest.raises(
        ValidationError, match="every selected Profile ancestor projection"
    ):
        ExecutionFinalization.model_validate_json(json.dumps(omitted))

    wire = result.model_dump(mode="json")
    wire["projected_traces"][0]["bundle_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="shared eligibility scope"):
        ExecutionFinalization.model_validate_json(json.dumps(wire))
