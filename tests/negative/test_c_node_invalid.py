import pytest
from fixtures.wp01c.nodes.cases import ISSUE, cases, content, parse, ref
from pydantic import ValidationError

from jimu_runtime.domain.nodes.context import ContextBuildOutput
from jimu_runtime.domain.nodes.decision import DecisionInput
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    DiscoveryOutput,
    ProfileHit,
    SkippedDiscoveryOutput,
)
from jimu_runtime.domain.nodes.envelopes import NodeOutputEnvelope
from jimu_runtime.domain.nodes.grounding import GroundingInput
from jimu_runtime.domain.nodes.observation import ObservationInput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput
from jimu_runtime.domain.runtime.applicability import ProfileEligibilityEvaluation
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import GroundingRecord, ObjectRecord


def test_pre_invocation_skip_prohibits_a_fabricated_attempt_and_empty_no_hits():
    wire = cases()
    with pytest.raises(ValidationError):
        parse(
            DiscoveryOutput, wire["skipped_discovery"] | {"attempt_id": "fake-attempt"}
        )
    with pytest.raises(ValidationError):
        parse(
            DiscoveryAttemptOutput,
            wire["discovery_output"]
            | {
                "outcome": "no_hits",
                "profile_hits": [],
                "model_invocation_refs": [],
            },
        )
    gate = parse(ProfileEligibilityEvaluation, wire["eligibility"])
    with pytest.raises(ValueError, match="empty eligible"):
        parse(SkippedDiscoveryOutput, wire["skipped_discovery"]).check_gate(gate)


def test_no_hits_and_hits_only_cover_eligible_profiles():
    wire = cases()
    gate = wire["eligibility"]
    ineligible = parse(
        ProfileEligibilityEvaluation,
        gate
        | {
            "eligible_profiles": [],
            "unresolved_profiles": gate["configured_profile_refs"],
        },
    )
    inputs = parse(DiscoveryInput, wire["discovery_input"])
    with pytest.raises(ValueError, match="eligible partition"):
        inputs.check_gate(ineligible)
    no_hits = parse(
        DiscoveryAttemptOutput,
        wire["discovery_output"]
        | {
            "outcome": "no_hits",
            "profile_hits": [],
        },
    )
    invocation = wire["discovery_invocation"] | {
        "profile_refs": [content("profile", "different-profile")],
    }
    with pytest.raises(ValueError, match="eligible and pinned"):
        no_hits.check_records(inputs, (), (parse(ModelInvocation, invocation),))


@pytest.mark.parametrize(
    "extra",
    (
        {"bbox": {"xyxy": [0, 0, 20, 20]}},
        {"object_id": "object-1"},
        {"violation_category": "parking"},
        {"excluded_profiles": []},
    ),
)
def test_profile_hits_cannot_carry_grounding_decision_or_gate_data(extra):
    hit = cases()["discovery_output"]["profile_hits"][0]
    with pytest.raises(ValidationError):
        parse(ProfileHit, hit | extra)


def test_n01_failure_cannot_invent_snapshot_and_duration_must_match_timing():
    wire = cases()
    with pytest.raises(ValidationError, match="context_snapshot_ref"):
        parse(
            ContextBuildOutput,
            wire["context_failure"]
            | {
                "context_snapshot_ref": ref("context_snapshot", "fake-snapshot"),
            },
        )
    with pytest.raises(ValidationError, match="elapsed"):
        parse(NodeOutputEnvelope, wire["output_envelope"] | {"duration_ms": 1})


def test_grounding_cannot_reselect_questions_or_move_object_to_derived_frame():
    wire = cases()
    grounding = wire["grounding_input"]
    item = grounding["profiles"][0]
    changed_hit = item["hit"] | {
        "effective_question_refs": list(
            reversed(item["hit"]["effective_question_refs"])
        )
    }
    inputs = parse(
        GroundingInput, grounding | {"profiles": [item | {"hit": changed_hit}]}
    )
    with pytest.raises(ValueError, match="inherit exactly"):
        inputs.check_discovery(parse(DiscoveryAttemptOutput, wire["discovery_output"]))
    obj = wire["object_record"]
    with pytest.raises(ValidationError, match="original image frame"):
        parse(
            ObjectRecord,
            obj
            | {
                "coordinate_system": obj["coordinate_system"]
                | {"frame": "derived_image"}
            },
        )
    with pytest.raises(ValidationError, match="dimensions"):
        parse(ObjectRecord, obj | {"bbox": {"xyxy": [1, 2, 9000, 9000]}})


def test_grounding_never_equates_failure_with_no_target():
    record = cases()["grounding_record"]
    with pytest.raises(ValidationError, match="diagnostics"):
        parse(GroundingRecord, record | {"outcome": "failed", "object_refs": []})
    with pytest.raises(ValidationError, match="references"):
        parse(GroundingRecord, record | {"outcome": "no_target"})


def test_batch_rejects_duplicate_missing_and_parse_failure_fact_results():
    batch = cases()["batch"]
    first = batch["question_results"][0]
    for bad in ([first], [first, first]):
        with pytest.raises(ValidationError, match="one result per effective Question"):
            parse(ObservationBatch, batch | {"question_results": bad})
    with pytest.raises(ValidationError, match="cannot contain fabricated Facts"):
        parse(
            ObservationBatch,
            batch
            | {
                "outcome": "parse_error",
                "envelope_validation": "untrusted",
                "association_validation": "unresolved",
                "validation_issues": [ISSUE],
            },
        )


def test_joint_attempt_rejects_an_additional_call_or_facts_from_another_attempt():
    wire = cases()
    inputs = parse(ObservationInput, wire["observation_input"])
    batch = parse(ObservationBatch, wire["batch"])
    invocation = parse(ModelInvocation, wire["observation_invocation"])
    with pytest.raises(ValueError, match="one joint"):
        inputs.check_batch(batch, (invocation, invocation), None)
    with pytest.raises(ValidationError, match="batch Attempt"):
        parse(
            ObservationBatch, wire["batch"] | {"attempt_ref": ref("attempt", "foreign")}
        )


def test_n05_prohibits_candidate_and_n06_requires_configured_mandatory_paths():
    wire = cases()
    with pytest.raises(ValidationError, match="candidate_prediction_ref"):
        parse(
            ToolNodeInput,
            wire["tool_input"]
            | {
                "candidate_prediction_ref": ref("candidate_prediction", "candidate-1"),
            },
        )
    configured = parse(DecisionInput, wire["decision_input"])
    modified = parse(
        DecisionInput, wire["decision_input"] | {"mandatory_rule_paths": []}
    )
    with pytest.raises(ValueError, match="complete resolved Profile scope"):
        modified.check_mandatory_scope(configured.mandatory_rule_paths)


def test_n06_rejects_a_selected_rule_evaluation_from_a_different_object():
    from fixtures.wp01c.nodes.cases import decision_case

    from jimu_runtime.domain.nodes.decision import DecisionOutput
    from jimu_runtime.domain.runtime.applicability import (
        RuleApplicabilityEvaluation,
        RuleSetApplicabilityEvaluation,
    )
    from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
    from jimu_runtime.domain.runtime.decisions import (
        CandidatePrediction,
        DecisionRecord,
    )

    wire = decision_case()
    inputs = parse(DecisionInput, wire["input"])
    output = parse(DecisionOutput, wire["output"])
    foreign_rule = parse(
        RuleApplicabilityEvaluation,
        wire["rule"] | {"object_ref": ref("object", "different-object")},
    )
    with pytest.raises(ValueError, match="different Object"):
        output.check_records(
            inputs,
            parse(DecisionRecord, wire["decision"]),
            parse(ExecutionCoverage, wire["coverage"]),
            parse(CandidatePrediction, wire["candidate"]),
            parse(ProfileEligibilityEvaluation, wire["gate"]),
            (parse(RuleSetApplicabilityEvaluation, wire["rule_set"]),),
            (foreign_rule,),
            inputs.mandatory_rule_paths,
        )


def test_generic_envelopes_cannot_bypass_node_boundaries():
    from fixtures.wp01c.nodes.cases import PIN

    wire = cases()
    for case, model, field, kind in (
        (
            "context_output",
            ContextBuildOutput,
            "output_artifacts",
            "profile_eligibility_evaluation",
        ),
        ("discovery_output", DiscoveryAttemptOutput, "output_artifacts", "object"),
        ("tool_input", ToolNodeInput, "upstream_artifacts", "candidate_prediction"),
    ):
        raw = wire[case]
        raw["envelope"][field].append({"ref": ref(kind, "out-of-scope"), "digest": PIN})
        with pytest.raises(ValidationError):
            parse(model, raw)


def test_n05_rejects_n03_grounding_tool_audit_even_for_the_same_tool_reference():
    from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
    from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
    from jimu_runtime.domain.nodes.tool_execution import ToolNodeOutput
    from jimu_runtime.domain.runtime.tool_executions import ToolExecution

    wire = cases()
    tool, raw_output = wire["grounding_tool"], wire["tool_output"]
    raw_output["summary"]["steps"] = [
        {
            "disposition": "invoked",
            "step_id": wire["tool_plan"]["steps"][0]["step_id"],
            "tool_execution_ref": ref("tool_execution", tool["tool_execution_id"]),
        }
    ]
    with pytest.raises(ValueError, match="actual bound ToolPlan step"):
        parse(ToolNodeOutput, raw_output).check_steps(
            parse(ToolNodeInput, wire["tool_input"]),
            parse(ProfileDefinition, wire["profile"]),
            parse(ToolPlanDefinition, wire["tool_plan"]),
            (parse(ToolExecution, tool),),
            (),
        )
