"""Domain round trips and the final N06 Coverage binding."""

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from jimu_runtime.domain.enums import DecisionResult
from jimu_runtime.domain.nodes.context import ContextBuildOutput
from jimu_runtime.domain.nodes.decision import DecisionOutput
from jimu_runtime.domain.nodes.discovery import DiscoveryOutput
from jimu_runtime.domain.nodes.grounding import GroundingOutput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeOutput
from jimu_runtime.domain.runtime.coverage import (
    CoverageMatch,
    CoverageStatus,
    EvaluatedCoverageScope,
    ExecutionCoverage,
    IncludedObligation,
    MandatoryRuleCoverage,
    MandatoryRuleObligation,
    ProfileCoverage,
    ProfileObligationTarget,
    SelectedApplicability,
)
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome
from jimu_runtime.workflow.engine import DecisionDraft
from jimu_runtime.workflow.providers import (
    SyntheticGroundingCase,
    build_context,
    coverage_ref,
    discover,
    finish_decision,
    ground,
)
from tests.fixtures.wp03.nodes.cases import (
    context,
    decision_case,
    discovery_input,
    grounding_input,
    observation_case,
    tools_case,
)
from tests.fixtures.wp03.nodes.scenario import VERSION, parking_fixture


def test_six_nodes_round_trip_and_bind_to_shared_immutable_coverage(
    tmp_path: Path,
) -> None:
    fixture = parking_fixture(tmp_path)
    scope, ctx = fixture.scope, context(fixture)
    at = scope.task.accepted_at
    first = build_context(scope, fixture.context_input(), at)
    assert (
        TypeAdapter(ContextBuildOutput).validate_json(first.output.model_dump_json())
        == first.output
    )
    discovery = discover(
        scope, discovery_input(fixture, ctx), ctx.snapshot, ctx.gate.evaluation, at
    )
    assert (
        TypeAdapter(DiscoveryOutput).validate_json(discovery.output.model_dump_json())
        == discovery.output
    )
    (profile,) = ctx.gate.evaluation.eligible_profiles
    grounded = ground(
        scope,
        grounding_input(fixture, discovery),
        discovery,
        at,
        cases=(SyntheticGroundingCase(profile_ref=profile, object_count=2),),
    )
    assert (
        GroundingOutput.model_validate_json(grounded.output.model_dump_json())
        == grounded.output
    )
    staged = []
    for obj in grounded.objects:
        _, observation = observation_case(fixture, ctx, obj, discovery)
        assert (
            ObservationBatch.model_validate_json(observation.batch.model_dump_json())
            == observation.batch
        )
        _, tools = tools_case(fixture, ctx, obj)
        assert (
            ToolNodeOutput.model_validate_json(tools.output.model_dump_json())
            == tools.output
        )
        decision = decision_case(fixture, ctx, obj, observation, tools)
        assert (
            DecisionDraft.model_validate_json(decision.draft.model_dump_json())
            == decision.draft
        )
        staged.append(decision)

    gate = ctx.gate.evaluation
    (profile_trace,) = ctx.gate.traces
    profile_obligation = MandatoryRuleObligation(
        obligation_id=staged[0].decision.explanation.obligation_ids[0],
        profile_ref=profile,
        target=ProfileObligationTarget(scope="profile"),
        outcome=IncludedObligation(
            disposition="included",
            selected_applicability=SelectedApplicability(
                evaluation_ref=gate.evaluation_ref, trace_ref=profile_trace.trace_ref
            ),
        ),
    )
    obligations = (
        profile_obligation,
        *(entry for item in staged for entry in item.obligations),
    )
    pin = {
        "schema_version": VERSION,
        "execution_id": scope.execution_id,
        "release_id": scope.pins.release_id,
        "bundle_digest": scope.pins.bundle_digest,
    }
    coverage = ExecutionCoverage.model_validate(
        {
            **pin,
            "coverage_id": coverage_ref(scope).record_id,
            "applicability_scope": EvaluatedCoverageScope(
                scope_status="evaluated",
                context_snapshot_ref=gate.context_snapshot_ref,
                profile_eligibility_evaluation_ref=gate.evaluation_ref,
                configured_profile_refs=(profile,),
            ),
            "discovery_outcome": DiscoveryOutcome.HITS,
            "discovery_model_invocation_refs": discovery.output.model_invocation_refs,
            "hit_profile_refs": (profile,),
            "profiles": (
                ProfileCoverage.model_validate(
                    {
                        **pin,
                        "profile_ref": profile,
                        "grounding_outcome": grounded.records[0].outcome,
                        "object_refs": grounded.records[0].object_refs,
                        "object_count": 2,
                        "obligation_ids": tuple(
                            item.obligation_id for item in obligations
                        ),
                        "coverage_status": CoverageStatus.COMPLETE,
                        "reasons": (),
                        "missing_evidence_refs": (),
                    }
                ),
            ),
            "mandatory_rule_coverage": MandatoryRuleCoverage(
                scan="all_mandatory", obligations=obligations
            ),
            "selected_applicability_evaluation_refs": tuple(
                item.outcome.selected_applicability.evaluation_ref
                for item in obligations
            ),
            "applicability_trace_refs": tuple(
                item.outcome.selected_applicability.trace_ref for item in obligations
            ),
            "unresolved_obligation_refs": (),
            "object_count": 2,
            "coverage_status": CoverageStatus.COMPLETE,
            "reasons": (),
            "missing_evidence_refs": (),
            "summary_result": DecisionResult.VIOLATION,
            "matches": tuple(
                CoverageMatch(
                    object_ref=item.decision.object_ref,
                    profile_ref=profile,
                    decision_ref=item.decision.decision_ref,
                    rule=path,
                )
                for item in staged
                for path in item.decision.conflict_resolution.matched_rules
            ),
            "created_at": at,
        }
    )
    for item in staged:
        output = finish_decision(scope, item, coverage, ctx.gate)
        assert DecisionOutput.model_validate_json(output.model_dump_json()) == output
        assert output.mandatory_rule_coverage == coverage.mandatory_rule_coverage
        assert output.execution_coverage_ref == coverage.coverage_ref
    with pytest.raises(ValueError, match="shared execution Coverage"):
        finish_decision(
            scope,
            staged[0],
            coverage.model_copy(update={"coverage_id": "unrelated"}),
            ctx.gate,
        )
