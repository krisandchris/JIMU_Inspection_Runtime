"""Dispatch claimed real NodeRuns to the scope-bound offline node adapters."""

from dataclasses import dataclass
from datetime import datetime

from jimu_runtime.bundle.artifacts import DRAFT_2020_12
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import ContextSchemaRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.nodes.context import ContextBuildInput
from jimu_runtime.domain.nodes.decision import DecisionInput
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    DiscoveryProfileBinding,
)
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput, NodeInputEnvelope
from jimu_runtime.domain.nodes.grounding import GroundingInput, GroundingProfileInput
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.authority import (
    ActorRef,
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import Attempt, NodeId, NodeRun
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    FactRecord,
    FactRef,
    ModelInvocationRef,
    PolicyContentRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import ToolPlanStepOrigin
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.mock_context import (
    build_context,
    snapshot_artifact,
    snapshot_ref,
)
from jimu_runtime.workflow.mock_decision import (
    SyntheticAuthority,
    mandatory_paths,
    stage_decision,
)
from jimu_runtime.workflow.mock_facts import observe, prepare_observation, run_tool_plan
from jimu_runtime.workflow.mock_perception import (
    discover,
    ground,
    original_coordinate_system,
)
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticArtifact,
    artifact,
    synthetic_id,
)
from jimu_runtime.workflow.worker_records import (
    ContextMessage,
    DecisionMessage,
    DiscoveryMessage,
    EligibilityMessage,
    FixtureEvidenceContent,
    GroundingMessage,
    NodeMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_source import OpenExecution, SyntheticObjectCase

_ACTOR = ActorRef(
    actor_kind="service", actor_id="jimu.workflow", identity_registry_id="jimu.runtime"
)


@dataclass(frozen=True, slots=True)
class NodeResult:
    message: NodeMessage
    artifacts: tuple[SyntheticArtifact, ...]
    model_refs: tuple[ModelInvocationRef, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservationRetryWitness:
    """A prior whole-batch failure committed with its exact N04 Attempt."""

    message: ObservationMessage
    node: NodeRun
    attempt: Attempt


@dataclass(slots=True)
class BranchInputs:
    context: ContextMessage | None = None
    gate: EligibilityMessage | None = None
    discovery: DiscoveryMessage | None = None
    grounding: GroundingMessage | None = None
    observation: ObservationMessage | None = None
    tool: ToolMessage | None = None
    observation_retry: ObservationRetryWitness | None = None

    def snapshot(self) -> ContextSnapshot:
        if self.context is None or self.context.snapshot is None:
            raise ValueError("workflow requires the authoritative ContextSnapshot")
        return self.context.snapshot

    def object_record(self, key: BranchKey) -> ObjectRecord:
        if self.grounding is None or key.object_id is None:
            raise ValueError("workflow requires an authoritative grounding Object")
        (obj,) = tuple(
            item for item in self.grounding.objects if item.object_id == key.object_id
        )
        return obj

    def selected_evidence(
        self, object_id: str | None = None
    ) -> tuple[EvidenceAsset, ...]:
        rows: dict[str, EvidenceAsset] = {}
        grounded: tuple[EvidenceAsset, ...] = ()
        if self.grounding is not None and object_id is not None:
            (obj,) = tuple(
                item for item in self.grounding.objects if item.object_id == object_id
            )
            (record,) = tuple(
                item
                for item in self.grounding.records
                if item.grounding_ref == obj.grounding_record_ref
            )
            refs = {
                ref.record_id for ref in (*obj.evidence_refs, *record.evidence_refs)
            }
            grounded = tuple(
                item
                for item in self.grounding.evidence
                if item.evidence_id in refs
                and item.scope.execution_id == obj.execution_id
                and (
                    item.scope.scope == "execution"
                    or (item.scope.object_ref.record_id, item.scope.profile_ref)
                    == (obj.object_id, obj.profile_ref)
                )
            )
        for item in (
            *grounded,
            *(() if self.observation is None else self.observation.evidence),
            *(
                ()
                if self.tool is None
                or self.tool.output.envelope.status is not NodeRunStatus.SUCCEEDED
                else self.tool.evidence
            ),
        ):
            if rows.setdefault(item.evidence_id, item) != item:
                raise ValueError(
                    "selected evidence conflicts for one immutable identity"
                )
        return tuple(rows.values())

    def selected_facts(
        self,
    ) -> tuple[tuple[FactRecord, ...], tuple[SyntheticAuthority, ...]]:
        observations = self.observation
        tools = self.tool
        if (
            tools is not None
            and tools.output.envelope.status is not NodeRunStatus.SUCCEEDED
        ):
            tools = None
        return (
            (
                *(() if observations is None else observations.facts),
                *(
                    ()
                    if tools is None
                    else (
                        fact
                        for fact in tools.facts
                        if fact.fact_id
                        in {
                            ref.record_id
                            for selection in tools.authorities
                            for ref in selection.selected_fact_refs
                        }
                    )
                ),
            ),
            (
                *(
                    ()
                    if observations is None or observations.authority is None
                    else (observations.authority,)
                ),
                *(() if tools is None else tools.authorities),
            ),
        )


def fact_artifact(fact: FactRecord) -> SyntheticArtifact:
    return artifact(K.FACT, fact.fact_id, fact)


def authority_artifact(selection: SyntheticAuthority) -> SyntheticArtifact:
    return artifact(K.AUTHORITATIVE_RESULT_SELECTION, selection.selection_id, selection)


def _fact_refs(facts: tuple[FactRecord, ...]) -> tuple[FactRef, ...]:
    return tuple(FactRef(record_kind=K.FACT, record_id=item.fact_id) for item in facts)


def _observation_authority(
    batch: ObservationMessage | None,
) -> tuple[SyntheticArtifact, ...]:
    return (
        () if batch is None or batch.authority is None else (batch.authority_artifact,)
    )


def _case(
    opened: OpenExecution, previous: BranchInputs, obj: ObjectRecord
) -> SyntheticObjectCase:
    if previous.grounding is None:
        raise ValueError("fixture Object ordering requires authoritative grounding")
    same_profile = tuple(
        item
        for item in previous.grounding.objects
        if item.profile_ref == obj.profile_ref
    )
    index = same_profile.index(obj)
    return next(
        (
            case
            for case in opened.scenario.objects
            if (case.profile_ref, case.object_index) == (obj.profile_ref, index)
        ),
        SyntheticObjectCase(obj.profile_ref, index),
    )


def build_context_node(
    opened: OpenExecution, envelope: NodeInputEnvelope, at: datetime
) -> NodeResult:
    scope, source = opened.scope, opened.source
    inputs = ContextBuildInput(
        schema_version=VERSION,
        envelope=envelope,
        accepted_task=scope.task,
        media_asset=scope.media,
        device_ref=source.device,
        camera_ref=source.camera,
        region_ref=opened.region_ref,
        context_schema_ref=ContextSchemaRef.model_validate(
            {
                **opened.region.context_schema_ref.model_dump(),
                "schema_dialect": DRAFT_2020_12,
            }
        ),
        context_provenance_policy_ref=PolicyContentRef.model_validate(
            opened.resolved.policy.context_provenance_policy_ref.model_dump()
        ),
    )
    result = build_context(scope, inputs, at)
    return NodeResult(
        ContextMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=result.output,
            snapshot=result.snapshot,
        ),
        result.artifacts,
    )


def discovery_inputs(
    opened: OpenExecution, previous: BranchInputs, env: NodeInputEnvelope
) -> DiscoveryInput:
    if previous.gate is None:
        raise ValueError("discovery requires the authoritative Profile gate")
    scope, snapshot, gate = (
        opened.scope,
        previous.snapshot(),
        previous.gate.evaluation,
    )
    profiles = {plan.rules.profile_ref: plan.definition for plan in opened.profiles}
    bindings = []
    for ref in gate.eligible_profiles:
        profile = profiles[ref]
        model = scope.resolve(profile.discovery_binding_ref, ModelBinding)
        prompt = model.prompt_template_ref
        if prompt is None:
            raise ValueError("discovery needs the admitted prompt binding")
        bindings.append(
            DiscoveryProfileBinding(
                profile_ref=ref,
                discovery_binding_ref=profile.discovery_binding_ref,
                prompt_template_ref=prompt,
                question_selection_policy_ref=PolicyContentRef.model_validate(
                    profile.question_selection_policy_ref.model_dump()
                ),
            )
        )
    return DiscoveryInput(
        schema_version=VERSION,
        envelope=env,
        original_media_ref=scope.media.media_ref,
        context_snapshot_ref=gate.context_snapshot_ref,
        context_snapshot_digest=snapshot_artifact(snapshot).pin.digest,
        profile_eligibility_evaluation_ref=gate.evaluation_ref,
        eligible_profiles=gate.eligible_profiles,
        bindings=tuple(bindings),
    )


def _discover(
    opened: OpenExecution, previous: BranchInputs, env: NodeInputEnvelope, at: datetime
) -> NodeResult:
    inputs = discovery_inputs(opened, previous, env)
    assert previous.gate is not None
    result = discover(
        opened.synthetic,
        inputs,
        previous.snapshot(),
        previous.gate.evaluation,
        at,
        hit_profiles=opened.scenario.discovery_hits,
        failed_profiles=opened.scenario.discovery_failures,
    )
    return NodeResult(
        DiscoveryMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=result.output,
            selections=result.selections,
            invocations=result.invocations,
        ),
        result.artifacts,
        result.output.model_invocation_refs,
    )


def _ground(
    opened: OpenExecution, previous: BranchInputs, env: NodeInputEnvelope, at: datetime
) -> NodeResult:
    if previous.discovery is None:
        raise ValueError("grounding requires authoritative Profile hits")
    scope = opened.synthetic
    definitions = {plan.rules.profile_ref: plan.definition for plan in opened.profiles}
    inputs = GroundingInput(
        schema_version=VERSION,
        envelope=env,
        original_media_ref=scope.media.media_ref,
        original_coordinate_system=original_coordinate_system(scope),
        profiles=tuple(
            GroundingProfileInput(
                hit=hit,
                grounding_binding=definitions[hit.profile_ref].grounding_binding,
            )
            for hit in previous.discovery.output.profile_hits
        ),
    )
    result = ground(
        scope,
        inputs,
        previous.discovery.result(),
        at,
        cases=opened.scenario.grounding,
    )
    return NodeResult(
        GroundingMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=result.output,
            records=result.records,
            objects=result.objects,
            invocations=result.invocations,
        ),
        result.artifacts,
        tuple(
            ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=item.model_invocation_id
            )
            for item in result.invocations
        ),
    )


def _observe(
    opened: OpenExecution,
    previous: BranchInputs,
    env: NodeInputEnvelope,
    obj: ObjectRecord,
    at: datetime,
) -> NodeResult:
    if previous.discovery is None:
        raise ValueError(
            "observation requires the durable effective Question selection"
        )
    (selection,) = tuple(
        item
        for item in previous.discovery.selections
        if item.selection_id == obj.question_selection_ref.record_id
    )
    scope, snapshot = opened.synthetic, previous.snapshot()
    inputs, _ = prepare_observation(scope, env, obj, snapshot, at)
    result = observe(
        scope,
        inputs,
        snapshot,
        selection,
        at,
        readings=_case(opened, previous, obj).readings,
    )
    authority = ObservationBatchSelection(
        schema_version=VERSION,
        execution_id=env.execution_id,
        profile_ref=obj.profile_ref,
        scope=FactScope.OBJECT,
        object_id=obj.object_id,
        selection_group="observation_batch",
        selection_id=synthetic_id(
            "observation-authority", env.attempt_id, obj.object_id
        ),
        selected_attempt_id=env.attempt_id,
        selected_result_ref=result.batch.batch_ref,
        selected_fact_refs=_fact_refs(
            tuple(item.primary_fact for item in result.batch.question_results)
        ),
        reason="validated_success",
        authority_revision=1,
        actor_ref=_ACTOR,
        created_at=at,
    )
    message = ObservationMessage(
        schema_version=VERSION,
        inputs=inputs,
        output=result.envelope,
        batch=result.batch,
        invocation=result.invocation,
        request_evidence=tuple(
            FixtureEvidenceContent.from_evidence(item) for item in result.evidence
        ),
        authority=authority,
    )
    return NodeResult(
        message,
        (*result.artifacts, *_observation_authority(message)),
        (result.batch.model_invocation_ref,),
    )


def _tools(
    opened: OpenExecution,
    previous: BranchInputs,
    env: NodeInputEnvelope,
    obj: ObjectRecord,
    at: datetime,
) -> NodeResult:
    scope, snapshot = opened.synthetic, previous.snapshot()
    profile = next(
        plan.definition
        for plan in opened.profiles
        if plan.rules.profile_ref == obj.profile_ref
    )
    inputs = ToolNodeInput(
        schema_version=VERSION,
        envelope=env,
        object_record=obj,
        image_view=ObjectImageView.for_object(obj),
        context_snapshot_ref=snapshot_ref(snapshot),
        tool_plan_ref=profile.tool_plan_ref,
        validated_observation_fact_refs=(),
    )
    result = run_tool_plan(
        scope, inputs, snapshot, at, outside=_case(opened, previous, obj).outside
    )
    authorities = []
    if result.output.envelope.status is NodeRunStatus.SUCCEEDED:
        for execution in result.executions:
            origin = execution.invocation_origin
            if not isinstance(origin, ToolPlanStepOrigin):
                raise ValueError(
                    "synthetic Tool authority requires the exact bound step"
                )
            if execution.status != "succeeded":
                continue
            authorities.append(
                ToolStepSelection(
                    schema_version=VERSION,
                    execution_id=env.execution_id,
                    profile_ref=obj.profile_ref,
                    scope=FactScope.OBJECT,
                    object_id=obj.object_id,
                    selection_group="tool_step",
                    selection_id=synthetic_id(
                        "tool-authority", env.attempt_id, obj.object_id, origin.step_id
                    ),
                    selected_attempt_id=env.attempt_id,
                    selected_result_ref=ToolExecutionRef(
                        record_kind=K.TOOL_EXECUTION,
                        record_id=execution.tool_execution_id,
                    ),
                    selected_fact_refs=execution.generated_fact_refs,
                    reason="validated_success",
                    authority_revision=1,
                    actor_ref=_ACTOR,
                    created_at=at,
                    tool_plan_ref=origin.tool_plan_ref,
                    step_id=origin.step_id,
                )
            )
    message = ToolMessage(
        schema_version=VERSION,
        inputs=inputs,
        output=result.output,
        executions=result.executions,
        facts=result.facts,
        authorities=tuple(authorities),
    )
    return NodeResult(message, (*result.artifacts, *message.authority_artifacts))


def _decision(
    opened: OpenExecution,
    previous: BranchInputs,
    env: NodeInputEnvelope,
    obj: ObjectRecord,
    at: datetime,
) -> NodeResult:
    if previous.gate is None:
        raise ValueError("decision must inherit the Pre-N02 Profile gate")
    scope, snapshot, gate = (
        opened.synthetic,
        previous.snapshot(),
        previous.gate.result(),
    )
    facts, authorities = previous.selected_facts()
    inputs = DecisionInput(
        schema_version=VERSION,
        envelope=env,
        object_record=obj,
        context_snapshot_ref=snapshot_ref(snapshot),
        profile_eligibility_evaluation_ref=gate.evaluation.evaluation_ref,
        mandatory_rule_paths=mandatory_paths(scope, obj),
        fact_refs=_fact_refs(facts),
        authority_selection_refs=tuple(
            AuthoritySelectionRef(
                record_kind=K.AUTHORITATIVE_RESULT_SELECTION,
                record_id=item.selection_id,
            )
            for item in authorities
        ),
    )
    result = stage_decision(
        scope, inputs, snapshot, gate, at, facts=facts, authorities=authorities
    )
    assert result.invocation is not None
    return NodeResult(
        DecisionMessage(
            schema_version=VERSION,
            draft=result.draft,
            obligations=result.obligations,
            traces=result.traces,
            invocation=result.invocation,
            evidence=result.evidence,
        ),
        result.artifacts,
        (
            ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION,
                record_id=result.invocation.model_invocation_id,
            ),
        ),
    )


def invocation_envelope(
    opened: OpenExecution, key: BranchKey, node: NodeRun, attempt: Attempt
) -> NodeInputEnvelope:
    scope, at = opened.scope, attempt.started_at
    if (
        at is None
        or key.node_id is not node.node_id
        or not attempt.belongs_to_node(node)
    ):
        raise ValueError(
            "workflow invocation requires a committed start and exact logical branch"
        )
    upstream = node.upstream_inputs
    if key.node_id is NodeId.N01:
        upstream = (artifact(K.TASK, scope.task.task_id, scope.task).pin,)
    return NodeInputEnvelope(
        schema_version=VERSION,
        node_id=node.node_id,
        task_id=scope.task.task_id,
        execution_id=node.execution_id,
        release_id=scope.pins.release_id,
        bundle_digest=scope.pins.bundle_digest,
        node_run_id=node.node_run_id,
        attempt_id=attempt.attempt_id,
        trace=node.trace,
        upstream_artifacts=upstream,
        node_config_ref=ContentRef.model_validate(node.config_ref.model_dump()),
    )


def failed_discovery(
    opened: OpenExecution,
    previous: BranchInputs,
    key: BranchKey,
    node: NodeRun,
    attempt: Attempt,
    failure: FailedNodeOutput,
) -> NodeResult:
    env = invocation_envelope(opened, key, node, attempt)
    inputs = discovery_inputs(opened, previous, env)
    outcome = DiscoveryAttemptOutput(
        schema_version=VERSION,
        outcome="failed",
        execution_id=env.execution_id,
        envelope=failure,
        profile_hits=(),
        diagnostics=(),
        model_invocation_refs=(),
    )
    outcome.check_records(inputs, (), ())
    return NodeResult(
        DiscoveryMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=outcome,
            selections=(),
            invocations=(),
        ),
        (),
    )


def invoke(
    opened: OpenExecution,
    previous: BranchInputs,
    key: BranchKey,
    node: NodeRun,
    attempt: Attempt,
) -> NodeResult:
    env = invocation_envelope(opened, key, node, attempt)
    at = attempt.started_at
    assert at is not None
    if key.node_id is NodeId.N01:
        return build_context_node(opened, env, at)
    if key.node_id is NodeId.N02:
        return _discover(opened, previous, env, at)
    if key.node_id is NodeId.N03:
        return _ground(opened, previous, env, at)
    obj = previous.object_record(key)
    if key.node_id is NodeId.N04:
        return _observe(opened, previous, env, obj, at)
    if key.node_id is NodeId.N05:
        return _tools(opened, previous, env, obj, at)
    return _decision(opened, previous, env, obj, at)
