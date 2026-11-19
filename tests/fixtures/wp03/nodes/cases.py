"""Typed inputs and selected synthetic attempts for the admitted fixture."""

from dataclasses import dataclass

from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.decision import DecisionInput
from jimu_runtime.domain.nodes.discovery import DiscoveryInput, DiscoveryProfileBinding
from jimu_runtime.domain.nodes.grounding import GroundingInput, GroundingProfileInput
from jimu_runtime.domain.nodes.observation import ObjectImageView, ObservationInput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput
from jimu_runtime.domain.runtime.authority import (
    ActorRef,
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    FactRecord,
    FactRef,
    PolicyContentRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.values import Value
from jimu_runtime.workflow.providers import (
    SyntheticAuthority,
    SyntheticDecisionResult,
    SyntheticDiscoveryResult,
    SyntheticGateResult,
    SyntheticObservationResult,
    SyntheticToolResult,
    artifact,
    build_context,
    build_gate,
    mandatory_paths,
    object_artifact,
    observe,
    original_coordinate_system,
    prepare_observation,
    record_digest,
    run_tool_plan,
    snapshot_artifact,
    snapshot_ref,
    stage_decision,
    synthetic_id,
)
from tests.fixtures.wp03.nodes.scenario import VERSION, ParkingFixture

K = RuntimeRecordKind
ACTOR = ActorRef(
    actor_kind="service", actor_id="fixture-worker", identity_registry_id="fixture-host"
)


@dataclass(frozen=True, slots=True)
class Context:
    snapshot: ContextSnapshot
    gate: SyntheticGateResult


def context(fixture: ParkingFixture) -> Context:
    scope = fixture.scope
    inputs = fixture.context_input()
    at = scope.task.accepted_at
    result = build_context(scope, inputs, at)
    assert (
        isinstance(result.output, SuccessfulContextBuild)
        and result.snapshot is not None
    )
    return Context(
        result.snapshot, build_gate(scope, inputs, result.output, result.snapshot, at)
    )


def discovery_input(fixture: ParkingFixture, ctx: Context) -> DiscoveryInput:
    scope = fixture.scope
    refs = ctx.gate.evaluation.eligible_profiles
    bindings = []
    for ref in refs:
        profile = scope.resolve(ref, ProfileDefinition)
        model = scope.resolve(profile.discovery_binding_ref, ModelBinding)
        assert model.prompt_template_ref is not None
        bindings.append(
            DiscoveryProfileBinding.model_validate(
                {
                    "profile_ref": ref,
                    "discovery_binding_ref": profile.discovery_binding_ref,
                    "prompt_template_ref": model.prompt_template_ref,
                    "question_selection_policy_ref": PolicyContentRef.model_validate(
                        profile.question_selection_policy_ref.model_dump()
                    ),
                }
            )
        )
    return DiscoveryInput(
        schema_version=VERSION,
        envelope=fixture.envelope(
            NodeId.N02, snapshot_artifact(ctx.snapshot), ctx.gate.artifacts[0]
        ),
        original_media_ref=scope.media.media_ref,
        context_snapshot_ref=snapshot_ref(ctx.snapshot),
        context_snapshot_digest=record_digest(ctx.snapshot),
        profile_eligibility_evaluation_ref=ctx.gate.evaluation.evaluation_ref,
        eligible_profiles=refs,
        bindings=tuple(bindings),
    )


def grounding_input(
    fixture: ParkingFixture, discovery: SyntheticDiscoveryResult
) -> GroundingInput:
    scope = fixture.scope
    return GroundingInput(
        schema_version=VERSION,
        envelope=fixture.envelope(NodeId.N03, *discovery.artifacts),
        original_media_ref=scope.media.media_ref,
        original_coordinate_system=original_coordinate_system(scope),
        profiles=tuple(
            GroundingProfileInput(
                hit=hit,
                grounding_binding=scope.resolve(
                    hit.profile_ref, ProfileDefinition
                ).grounding_binding,
            )
            for hit in discovery.output.profile_hits
        ),
    )


def observation_case(
    fixture: ParkingFixture,
    ctx: Context,
    obj: ObjectRecord,
    discovery: SyntheticDiscoveryResult,
    *,
    readings: tuple[Value, ...] | None = None,
) -> tuple[ObservationInput, SyntheticObservationResult]:
    scope, snapshot = fixture.scope, ctx.snapshot
    (selection,) = tuple(
        item for item in discovery.selections if item.profile_ref == obj.profile_ref
    )
    envelope = fixture.envelope(
        NodeId.N04,
        object_artifact(obj),
        snapshot_artifact(snapshot),
        artifact(K.QUESTION_SELECTION_RECORD, selection.selection_id, selection),
        branch=obj.object_id,
    )
    inputs, _ = prepare_observation(
        scope, envelope, obj, snapshot, scope.task.accepted_at
    )
    return inputs, observe(
        scope, inputs, snapshot, selection, scope.task.accepted_at, readings=readings
    )


def tools_case(
    fixture: ParkingFixture,
    ctx: Context,
    obj: ObjectRecord,
    *,
    outside: bool | None = True,
) -> tuple[ToolNodeInput, SyntheticToolResult]:
    scope, snapshot = fixture.scope, ctx.snapshot
    profile = scope.resolve(obj.profile_ref, ProfileDefinition)
    inputs = ToolNodeInput(
        schema_version=VERSION,
        envelope=fixture.envelope(
            NodeId.N05,
            object_artifact(obj),
            snapshot_artifact(snapshot),
            branch=obj.object_id,
        ),
        object_record=obj,
        image_view=ObjectImageView(roi=obj.bbox, transforms=()),
        context_snapshot_ref=snapshot_ref(snapshot),
        tool_plan_ref=profile.tool_plan_ref,
        validated_observation_fact_refs=(),
    )
    return inputs, run_tool_plan(
        scope, inputs, snapshot, scope.task.accepted_at, outside=outside
    )


def authority_cases(
    fixture: ParkingFixture,
    obj: ObjectRecord,
    observation: SyntheticObservationResult,
    tools: SyntheticToolResult,
) -> tuple[tuple[FactRecord, ...], tuple[SyntheticAuthority, ...]]:
    scope = fixture.scope
    batch = observation.batch
    obs_facts = tuple(result.primary_fact for result in batch.question_results)
    selected: list[SyntheticAuthority] = [
        ObservationBatchSelection(
            schema_version=VERSION,
            selection_id=synthetic_id("test-selection", batch.observation_batch_id),
            selection_group="observation_batch",
            scope=FactScope.OBJECT,
            execution_id=scope.execution_id,
            object_id=obj.object_id,
            profile_ref=obj.profile_ref,
            selected_attempt_id=batch.attempt_ref.record_id,
            selected_result_ref=batch.batch_ref,
            selected_fact_refs=tuple(
                FactRef(record_kind=K.FACT, record_id=fact.fact_id)
                for fact in obs_facts
            ),
            reason="validated_success",
            authority_revision=1,
            actor_ref=ACTOR,
            created_at=scope.task.accepted_at,
        )
    ]
    for execution in tools.executions:
        if execution.status == "succeeded":
            origin = execution.invocation_origin
            assert origin.kind == "tool_plan_step"
            selected.append(
                ToolStepSelection(
                    schema_version=VERSION,
                    selection_id=synthetic_id(
                        "test-selection", execution.tool_execution_id
                    ),
                    selection_group="tool_step",
                    scope=FactScope.OBJECT,
                    execution_id=scope.execution_id,
                    object_id=obj.object_id,
                    profile_ref=obj.profile_ref,
                    selected_attempt_id=execution.attempt_ref.record_id,
                    selected_result_ref=ToolExecutionRef(
                        record_kind=K.TOOL_EXECUTION,
                        record_id=execution.tool_execution_id,
                    ),
                    selected_fact_refs=execution.generated_fact_refs,
                    tool_plan_ref=origin.tool_plan_ref,
                    step_id=origin.step_id,
                    reason="validated_success",
                    authority_revision=1,
                    actor_ref=ACTOR,
                    created_at=scope.task.accepted_at,
                )
            )
    return (*obs_facts, *tools.facts), tuple(selected)


def decision_input(
    fixture: ParkingFixture,
    ctx: Context,
    obj: ObjectRecord,
    facts: tuple[FactRecord, ...],
    authorities: tuple[SyntheticAuthority, ...],
) -> DecisionInput:
    scope = fixture.scope
    return DecisionInput(
        schema_version=VERSION,
        envelope=fixture.envelope(
            NodeId.N06,
            object_artifact(obj),
            snapshot_artifact(ctx.snapshot),
            ctx.gate.artifacts[0],
            *(artifact(K.FACT, fact.fact_id, fact) for fact in facts),
            *(
                artifact(K.AUTHORITATIVE_RESULT_SELECTION, item.selection_id, item)
                for item in authorities
            ),
            branch=obj.object_id,
        ),
        object_record=obj,
        context_snapshot_ref=snapshot_ref(ctx.snapshot),
        profile_eligibility_evaluation_ref=ctx.gate.evaluation.evaluation_ref,
        mandatory_rule_paths=mandatory_paths(scope, obj),
        fact_refs=tuple(
            FactRef(record_kind=K.FACT, record_id=fact.fact_id) for fact in facts
        ),
        authority_selection_refs=tuple(
            AuthoritySelectionRef(
                record_kind=K.AUTHORITATIVE_RESULT_SELECTION,
                record_id=item.selection_id,
            )
            for item in authorities
        ),
    )


def decision_case(
    fixture: ParkingFixture,
    ctx: Context,
    obj: ObjectRecord,
    observation: SyntheticObservationResult,
    tools: SyntheticToolResult,
) -> SyntheticDecisionResult:
    facts, authorities = authority_cases(fixture, obj, observation, tools)
    inputs = decision_input(fixture, ctx, obj, facts, authorities)
    return stage_decision(
        fixture.scope,
        inputs,
        ctx.snapshot,
        ctx.gate,
        fixture.scope.task.accepted_at,
        facts=facts,
        authorities=authorities,
    )
