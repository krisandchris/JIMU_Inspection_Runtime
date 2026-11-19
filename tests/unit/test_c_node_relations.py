from fixtures.wp01c.nodes.cases import cases, parse

from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import FactStatus
from jimu_runtime.domain.nodes.context import ContextBuildInput, SuccessfulContextBuild
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    SkippedDiscoveryOutput,
)
from jimu_runtime.domain.nodes.grounding import GroundingInput, GroundingOutput
from jimu_runtime.domain.nodes.observation import ObservationInput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput, ToolNodeOutput
from jimu_runtime.domain.runtime.applicability import ProfileEligibilityEvaluation
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    GroundingRecord,
    ObjectRecord,
)
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import ToolExecution


def test_context_resolves_the_verifiable_n01_pins():
    wire = cases()
    inputs = parse(ContextBuildInput, wire["context_input"])
    output = parse(SuccessfulContextBuild, wire["context_output"])
    snapshot = parse(ContextSnapshot, wire["snapshot"])
    output.check_snapshot(inputs, snapshot)


def test_eligible_discovery_selection_passthrough_and_tool_grounding():
    wire = cases()
    discovery_input = parse(DiscoveryInput, wire["discovery_input"])
    discovery_output = parse(DiscoveryAttemptOutput, wire["discovery_output"])
    selection = parse(QuestionSelectionRecord, wire["selection"])
    profile = parse(ProfileDefinition, wire["profile"])
    discovery_input.check_gate(parse(ProfileEligibilityEvaluation, wire["eligibility"]))
    discovery_input.check_profiles((profile,))
    discovery_output.check_records(
        discovery_input,
        (selection,),
        (parse(ModelInvocation, wire["discovery_invocation"]),),
    )
    grounding_input = parse(GroundingInput, wire["grounding_input"])
    grounding_input.check_discovery(discovery_output)
    grounding_output = parse(GroundingOutput, wire["grounding_output"])
    grounding_output.check_records(
        grounding_input,
        (parse(GroundingRecord, wire["grounding_record"]),),
        (parse(ObjectRecord, wire["object_record"]),),
        (selection,),
        (parse(ToolExecution, wire["grounding_tool"]),),
        (profile,),
    )


def test_empty_eligibility_and_actual_no_hits_are_distinct():
    wire = cases()
    gate = wire["eligibility"]
    actual = parse(ProfileEligibilityEvaluation, gate)
    skipped = parse(SkippedDiscoveryOutput, wire["skipped_discovery"])
    excluded = parse(
        ProfileEligibilityEvaluation,
        gate
        | {
            "eligible_profiles": [],
            "excluded_profiles": gate["configured_profile_refs"],
        },
    )
    skipped.check_gate(excluded)
    inputs = parse(DiscoveryInput, wire["discovery_input"])
    inputs.check_gate(actual)
    no_hits = parse(
        DiscoveryAttemptOutput,
        wire["discovery_output"] | {"outcome": "no_hits", "profile_hits": []},
    )
    no_hits.check_records(
        inputs, (), (parse(ModelInvocation, wire["discovery_invocation"]),)
    )


def test_grounding_distinguishes_no_target_failed_and_partial():
    record = cases()["grounding_record"]
    for outcome, issue_count in (("no_target", 0), ("failed", 1), ("partial", 1)):
        issue = {
            "code": "diagnostic",
            "path": [],
            "message": "Incomplete grounding",
            "related_refs": [],
        }
        parsed = parse(
            GroundingRecord,
            record
            | {
                "outcome": outcome,
                "object_refs": record["object_refs"] if outcome == "partial" else [],
                "validation_issues": [issue] * issue_count,
            },
        )
        assert parsed.as_profile_result().outcome is GroundingOutcome(outcome)


def test_joint_observation_accepts_independent_sibling_error_and_projects_authority_input():
    wire = cases()
    inputs = parse(ObservationInput, wire["observation_input"])
    batch = parse(ObservationBatch, wire["batch"])
    invocation = parse(ModelInvocation, wire["observation_invocation"])
    inputs.check_batch(batch, (invocation,), None)
    assert tuple(result.primary_fact.status for result in batch.question_results) == (
        FactStatus.KNOWN,
        FactStatus.ERROR,
    )
    descriptor = batch.to_authority_descriptor()
    assert descriptor.result_ref == batch.batch_ref
    assert descriptor.question_results[0].fact_ref.record_id == "fact-0"
    assert descriptor.question_results[1].fact_ref.record_id == "fact-1"


def test_n05_checks_the_pinned_plan_without_any_n06_record():
    wire = cases()
    inputs = parse(ToolNodeInput, wire["tool_input"])
    output = parse(ToolNodeOutput, wire["tool_output"])
    plan = parse(ToolPlanDefinition, wire["tool_plan"])
    profile = parse(ProfileDefinition, wire["profile"])
    inputs.check_observation_facts(plan, (), None)
    output.check_steps(inputs, profile, plan, (), ())


def test_n06_failed_advisory_call_does_not_remove_a_mandatory_evaluation():
    from fixtures.wp01c.nodes.cases import decision_case

    from jimu_runtime.domain.nodes.decision import DecisionInput, DecisionOutput
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
    output.check_records(
        inputs,
        parse(DecisionRecord, wire["decision"]),
        parse(ExecutionCoverage, wire["coverage"]),
        parse(CandidatePrediction, wire["candidate"]),
        parse(ProfileEligibilityEvaluation, wire["gate"]),
        (parse(RuleSetApplicabilityEvaluation, wire["rule_set"]),),
        (parse(RuleApplicabilityEvaluation, wire["rule"]),),
        inputs.mandatory_rule_paths,
    )


def test_n04_whole_batch_retry_preserves_questions_and_uses_new_identifiers():
    from fixtures.wp01c.nodes.cases import ISSUE, ref

    wire = cases()
    first, inputs, call = (
        wire["batch"],
        wire["observation_input"],
        wire["observation_invocation"],
    )
    failed = parse(
        ObservationBatch,
        first
        | {
            "outcome": "parse_error",
            "envelope_validation": "untrusted",
            "association_validation": "unresolved",
            "question_results": [],
            "validation_issues": [ISSUE],
        },
    )
    retry_ref = ref("attempt", "retry-attempt")
    new_batch_ref = ref("observation_batch", "retry-batch")
    retry_model_ref = ref("model_invocation", "retry-model")
    inputs["envelope"]["attempt_id"] = retry_ref["record_id"]
    inputs["retry"] = {
        "previous_batch_ref": failed.batch_ref.model_dump(mode="json"),
        "reason": "Explicit whole batch retry",
    }
    new_batch = first | {
        "retry_of_batch_ref": failed.batch_ref.model_dump(mode="json"),
        "observation_batch_id": new_batch_ref["record_id"],
        "attempt_ref": retry_ref,
        "model_invocation_ref": retry_model_ref,
    }
    for result in new_batch["question_results"]:
        result["primary_fact"]["attempt_id"] = retry_ref["record_id"]
        result["primary_fact"]["source"].update(
            observation_batch_ref=new_batch_ref,
            model_invocation_ref=retry_model_ref,
        )
    retry_call = call | {
        "model_invocation_id": retry_model_ref["record_id"],
        "attempt_ref": retry_ref,
    }
    parse(ObservationInput, inputs).check_batch(
        parse(ObservationBatch, new_batch),
        (parse(ModelInvocation, retry_call),),
        failed,
    )


def test_n05_resolves_actual_tool_plan_step_audit():
    from fixtures.wp01c.nodes.cases import ref

    wire = cases()
    raw_inputs, raw_output, tool = (
        wire["tool_input"],
        wire["tool_output"],
        wire["step_tool"],
    )
    raw_inputs["envelope"]["attempt_id"] = tool["attempt_ref"]["record_id"]
    raw_output["envelope"]["attempt_id"] = tool["attempt_ref"]["record_id"]
    raw_output["summary"]["steps"] = [
        {
            "disposition": "invoked",
            "step_id": tool["invocation_origin"]["step_id"],
            "tool_execution_ref": ref("tool_execution", tool["tool_execution_id"]),
        }
    ]
    parse(ToolNodeOutput, raw_output).check_steps(
        parse(ToolNodeInput, raw_inputs),
        parse(ProfileDefinition, wire["profile"]),
        parse(ToolPlanDefinition, wire["tool_plan"]),
        (parse(ToolExecution, tool),),
        (),
    )
