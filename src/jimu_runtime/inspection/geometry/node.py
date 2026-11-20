"""Configured N05 over verified images and fence-selected ToolPlan children."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import TypeAdapter

from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    ContextSource,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.resources import StaticAssetDescriptor
from jimu_runtime.domain.definitions.tools import (
    ToolDefinition,
    ToolPlanDefinition,
    ToolStep,
    plan_ancestors,
)
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.enums import RuntimeRecordKind as K
from jimu_runtime.domain.fact_selectors import ToolProducerSelector
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.envelopes import (
    FailedNodeOutput,
    NodeError,
    NodeInputEnvelope,
    NodeOutputEnvelope,
    SucceededNodeOutput,
)
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
    ToolPlanStepResult,
    UninvokedToolStep,
    matches_observation_selector,
    observation_facts,
)
from jimu_runtime.domain.policies import (
    EvidencePolicy,
    EvidencePolicyArtifact,
    ToolFailurePolicy,
    ToolFailurePolicyArtifact,
    ToolSkipPolicy,
    ToolSkipPolicyArtifact,
)
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    RetentionPolicyRef,
)
from jimu_runtime.domain.runtime.executions import NodeId, PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    EvidenceRef,
    FactRecord,
    FactRef,
    KnownFact,
    ModelInvocationRef,
    ObjectRef,
    PolicyContentRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.intake import (
    IdempotencyScope,
    RegionDefinitionProvenance,
)
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    KnownToolOutputState,
    ReturnedToolAttempt,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.tool_mappings import (
    ContextInput,
    FactInput,
    ImageInput,
    ObjectInput,
    StepOutputInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ToolStepGuardConditionContext,
)
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.values import ListValue, RecordValue, Value
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.decision.applicability import (
    ApplicabilityEvaluator,
    MetadataInputs,
    type_bindings,
)
from jimu_runtime.inspection.geometry import (
    GEOMETRY_SLOT,
    TACTILE_MASK,
    VERSION,
    WHEEL_MASK,
    EvidenceBytes,
    GeometryInput,
    GeometrySidecar,
    MaskInput,
    ParkingArea,
    verify_parking_area,
)
from jimu_runtime.inspection.media import (
    LocalMediaStore,
    MaskSidecarRecord,
    MediaError,
    ToolSidecarSlot,
    ToolSidecarSpec,
    VerifiedEvidence,
    VerifiedOriginal,
)
from jimu_runtime.inspection.media.original_evidence import original_image_evidence
from jimu_runtime.inspection.rules import PredicateInputError, PreparedPredicate
from jimu_runtime.tools.executor import AncestorToolEvidence
from jimu_runtime.tools.registry import ToolDispatch
from jimu_runtime.tools.step_attempts import RecoveredToolStep, StepCompletion
from jimu_runtime.workflow.mock_context import snapshot_artifact, snapshot_ref
from jimu_runtime.workflow.mock_support import SyntheticArtifact, artifact
from jimu_runtime.workflow.tool_dependencies import (
    guard_fact_selectors,
    needs_observation,
)
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode
from jimu_runtime.workflow.worker_nodes import NodeResult, invocation_envelope
from jimu_runtime.workflow.worker_records import (
    ObservationMessage,
    ToolMessage,
    tool_plan_completion_artifact,
)

_SLOT = TypeAdapter[ToolSidecarSlot](ToolSidecarSlot)
_OUTPUT_SLOTS: dict[str, ToolSidecarSlot] = {
    "wheel_segmentation": WHEEL_MASK,
    "tactile_paving_segmentation": TACTILE_MASK,
    "wheel_boundary_geometry": GEOMETRY_SLOT,
}


def _issue(step: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=(step,), related_refs=())


def _record_value(value: Value | None, path: tuple[str, ...]) -> Value | None:
    for part in path:
        if not isinstance(value, RecordValue):
            return None
        value = value.value.get(part)
    return value


def _plain(value: Value) -> object:
    if isinstance(value, RecordValue):
        return {key: _plain(item) for key, item in value.value.items()}
    if isinstance(value, ListValue):
        return [_plain(item) for item in value.value]
    return value.value


def _inputs(
    claimed: ClaimedNode,
    envelope: NodeInputEnvelope,
    plan: ToolPlanDefinition,
    profile: ProfileDefinition,
    observation: ObservationMessage | None,
    step: ToolStep | None = None,
) -> tuple[ToolNodeInput, tuple[FactRecord, ...]]:
    previous = claimed.previous
    snapshot = previous.snapshot()
    obj = previous.object_record(claimed.branch)
    scope = claimed.opened.scope
    scope.check_snapshot(snapshot, snapshot_ref(snapshot))
    if profile.tool_plan_ref is None or obj.source_media_ref != scope.media.media_ref:
        raise AdapterRejected("geometry.object_plan_or_media_unavailable")
    legacy_fact_ids = {
        pin.ref.record_id
        for pin in envelope.upstream_artifacts
        if pin.ref.record_kind is K.FACT
    }
    input_steps = (step,) if step is not None else plan.steps if legacy_fact_ids else ()
    guards = tuple(
        selector
        for input_step in input_steps
        for selector in guard_fact_selectors(
            scope.resolve(input_step.skip_policy_ref, ToolSkipPolicyArtifact).policy
        )
    )
    facts = (
        ()
        if observation is None or observation.authority is None
        else observation_facts(input_steps, observation.facts, guards)
    )
    if legacy_fact_ids:
        facts = tuple(fact for fact in facts if fact.fact_id in legacy_fact_ids)
    authority = None if observation is None or not facts else observation.authority
    values: dict[str, object] = {
        "schema_version": VERSION,
        "envelope": envelope,
        "object_record": obj,
        "image_view": ObjectImageView.for_object(obj),
        "context_snapshot_ref": snapshot_ref(snapshot),
        "tool_plan_ref": profile.tool_plan_ref,
        "validated_observation_fact_refs": tuple(
            FactRef(record_kind=K.FACT, record_id=fact.fact_id) for fact in facts
        ),
    }
    pins = {
        artifact(K.OBJECT, obj.object_id, obj).pin,
        snapshot_artifact(snapshot).pin,
    }
    fact_pins = tuple(artifact(K.FACT, fact.fact_id, fact).pin for fact in facts)
    if authority is not None:
        values["observation_authority_ref"] = AuthoritySelectionRef(
            record_kind=K.AUTHORITATIVE_RESULT_SELECTION,
            record_id=authority.selection_id,
        )
        assert observation is not None
        fact_pins = (*fact_pins, observation.authority_artifact.pin)
    upstream = {pin.stable_json() for pin in envelope.upstream_artifacts}
    if (
        not {pin.stable_json() for pin in pins} <= upstream
        or legacy_fact_ids
        and not {pin.stable_json() for pin in fact_pins} <= upstream
    ):
        raise AdapterRejected("geometry.input_projection_unpinned")
    if step is not None and not legacy_fact_ids:
        supplemental = tuple(
            PinnedRuntimeInput(ref=pin.ref, digest=pin.digest) for pin in fact_pins
        )
        values["envelope"] = envelope.model_copy(
            update={"upstream_artifacts": (*envelope.upstream_artifacts, *supplemental)}
        )
    inputs = ToolNodeInput.model_validate(values)
    inputs.check_plan(profile, plan)
    inputs.check_observation_facts(plan, facts, authority, guard_selectors=guards)
    return inputs, facts


def _parking_area(
    claimed: ClaimedNode, original: VerifiedOriginal
) -> ParkingArea | None:
    opened, snapshot = claimed.opened, claimed.previous.snapshot()
    region = opened.scope.resolve(snapshot.region_ref, RegionDefinition)
    if (
        snapshot.region_ref != opened.region_ref
        or region != opened.region
        or region.geometry_ref is None
    ):
        return None
    geometry = opened.scope.resolve(region.geometry_ref, GeometryResourceDefinition)
    entry = opened.scope.catalogue.resolve(geometry.provenance.source_asset_ref)
    if not isinstance(entry.value, StaticAssetDescriptor) or entry.asset_bytes is None:
        return None
    try:
        return verify_parking_area(
            catalogue=opened.scope.catalogue,
            region_ref=snapshot.region_ref,
            region=region,
            geometry_ref=region.geometry_ref,
            geometry=geometry,
            source=entry.value,
            source_bytes=entry.asset_bytes,
            coordinates=original.coordinate_system,
        )
    except ValueError:
        return None


def _original_evidence(
    claimed: ClaimedNode,
    original: VerifiedOriginal,
    policy_ref: PolicyContentRef,
    policy: EvidencePolicy,
) -> EvidenceBytes:
    return EvidenceBytes(
        original_image_evidence(
            claimed.opened.scope.task,
            claimed.execution.execution_id,
            original.asset,
            original.coordinate_system,
            policy_ref,
            policy,
        ),
        original.content,
    )


async def _stage(
    media: LocalMediaStore,
    scope: IdempotencyScope,
    dispatch: ToolDispatch,
    sidecar: GeometrySidecar,
    retention: RetentionPolicyRef,
    policy: EvidencePolicy,
) -> EvidenceAsset:
    slot = _SLOT.validate_python(sidecar.slot)
    evidence_id = f"{dispatch.tool_execution_id}.{slot}"
    mask = None
    if slot != GEOMETRY_SLOT:
        mask = MaskSidecarRecord(
            coordinate_system=sidecar.spatial.transforms[0].source,
            evidence_id=evidence_id,
            sha256=sidecar.sha256,
            source_media_sha256=sidecar.spatial.original_media_ref.sha256,
        )
    return await media.stage_tool_sidecar(
        scope,
        dispatch.execution_id,
        dispatch.tool_execution_id,
        slot,
        ToolSidecarSpec(
            evidence_id=evidence_id,
            spatial=sidecar.spatial,
            retention=retention,
            access=policy.access_scope,
            mask_record=mask,
            geometry_digest=sidecar.sha256 if slot == GEOMETRY_SLOT else None,
        ),
        sidecar.content,
    )


def _named_asset(completion: StepCompletion, slot: str) -> EvidenceAsset | None:
    returned = completion.result.execution.attempt_result
    if not isinstance(returned, ReturnedToolAttempt) or isinstance(
        returned.result, FailedToolResult
    ):
        return None
    refs = next(
        (item.evidence_refs for item in returned.result.evidence if item.name == slot),
        (),
    )
    if len(refs) != 1:
        return None
    return next(
        (
            item
            for item in completion.result.evidence
            if item.evidence_id == refs[0].record_id
        ),
        None,
    )


@dataclass(slots=True)
class _Round:
    ancestors: dict[str, frozenset[str]]
    completions: dict[str, StepCompletion] = field(default_factory=dict)
    sidecars: dict[str, VerifiedEvidence] = field(default_factory=dict)
    outcomes: dict[str, ToolPlanStepResult] = field(default_factory=dict)
    issues: list[ValidationIssue] = field(default_factory=list)
    recovered: dict[str, RecoveredToolStep] = field(default_factory=dict)
    observation_input_artifacts: dict[PinnedRuntimeInput, None] = field(
        default_factory=dict
    )
    publication_blocked: bool = False

    def dependencies_available(
        self, step: ToolStep, failures: dict[str, ToolFailurePolicy]
    ) -> bool:
        for parent in step.depends_on:
            result = self.completions.get(parent)
            if (result is None or result.selection is None) and (
                step.requirement == "required"
                or failures[parent].optional_continuation == "block"
            ):
                return False
        return True

    def record(self, step: ToolStep, completion: StepCompletion) -> None:
        self.completions[step.step_id] = completion
        self.outcomes[step.step_id] = InvokedToolStep(
            disposition="invoked",
            step_id=step.step_id,
            tool_execution_ref=ToolExecutionRef(
                record_kind=K.TOOL_EXECUTION,
                record_id=completion.result.execution.tool_execution_id,
            ),
        )

    def block(self, step: ToolStep, code: str, message: str) -> None:
        issue = _issue(step.step_id, code, message)
        self.issues.append(issue)
        archived = self.recovered.get(step.step_id)
        if archived is not None:
            self.record(step, archived.completion)
            self.observation_input_artifacts.update(
                dict.fromkeys(archived.verified_observation_inputs)
            )
            self.publication_blocked = True
            return
        self.outcomes[step.step_id] = UninvokedToolStep(
            disposition="blocked",
            step_id=step.step_id,
            reason_code=code,
            validation_issues=(issue,),
        )


def _step_guard(
    claimed: ClaimedNode,
    inputs: ToolNodeInput,
    facts: tuple[FactRecord, ...],
    step: ToolStep,
    policy: ToolSkipPolicy,
    state: _Round,
) -> RuleTruth:
    condition = policy.condition_ast
    if condition is True:
        return RuleTruth.TRUE
    context = claimed.previous.context
    if context is None or not isinstance(context.output, SuccessfulContextBuild):
        return RuleTruth.UNKNOWN
    opened, obj = claimed.opened, inputs.object_record
    evaluator = ApplicabilityEvaluator(
        opened,
        MetadataInputs(claimed.previous.snapshot(), context.output),
        type_bindings(opened),
    )
    use = evaluator.use(obj.profile_ref)
    refs = condition_references(condition)
    ancestors = state.ancestors[step.step_id]
    ancestor_keys = {
        (selector.scope, selector.fact_key) for selector in refs.local_tool_selectors
    } | {
        (selector.scope, selector.fact_key)
        for selector in refs.fact_selectors
        if isinstance(selector.producer_selector, ToolProducerSelector)
    }
    operands = (
        *(
            fact
            for fact in facts
            if any(
                matches_observation_selector(fact, selector)
                for selector in refs.fact_selectors
            )
        ),
        *(
            fact
            for step_id, completion in state.completions.items()
            if step_id in ancestors and completion.selection is not None
            for fact in completion.result.facts
            if (fact.scope, fact.fact_key) in ancestor_keys
        ),
    )
    try:
        predicate = PreparedPredicate(
            catalogue=opened.scope.catalogue,
            input_domain=ConditionInputDomain.TOOL_CONDITION,
            context=ToolStepGuardConditionContext.model_validate(
                {
                    "use_role": "tool_step_guard",
                    "profile_ref": obj.profile_ref.model_dump(),
                    "policy_ref": step.skip_policy_ref,
                    "tool_plan_ref": inputs.tool_plan_ref,
                    "step_id": step.step_id,
                    "context_registrations": use.context_registrations,
                    "descriptors": opened.resolved.schema_descriptors,
                    **(
                        {"region_ref": use.region_ref}
                        if use.region_ref is not None
                        else {}
                    ),
                }
            ),
            bindings=evaluator.bindings,
            fact_schemas=opened.resolved.fact_schemas,
        )
        evaluated = predicate.evaluate(
            execution_id=obj.execution_id,
            object_id=obj.object_id,
            context_fields=evaluator.fields(obj.profile_ref, refs.context_selectors),
            facts=operands,
        )
        if {
            "predicate.context.missing",
            "predicate.fact.missing",
            "predicate.fact.missing_field",
        }.intersection(evaluated.diagnostic_codes):
            return RuleTruth.UNKNOWN
        return evaluated.truth
    except PredicateInputError:
        return RuleTruth.UNKNOWN


def _source_field(
    source: ContextInput | FactInput | ImageInput | ObjectInput | StepOutputInput,
    inputs: ToolNodeInput,
    claimed: ClaimedNode,
    facts: tuple[FactRecord, ...],
    original: VerifiedOriginal,
    region: ParkingArea | None,
    state: _Round,
    target: tuple[str, ...],
) -> tuple[object | None, MaskInput | None]:
    if isinstance(source, ImageInput):
        if source.selector.field_path == ():
            return original.asset.media_ref.model_dump(mode="json"), None
        return None, None
    if isinstance(source, ObjectInput):
        if source.selector.field_path == ("bbox",):
            return inputs.object_record.bbox.model_dump(mode="json"), None
        return None, None
    if isinstance(source, FactInput):
        fact = next(
            (
                item
                for item in facts
                if matches_observation_selector(item, source.selector)
            ),
            None,
        )
        typed = (
            _record_value(fact.value, source.selector.field_path)
            if isinstance(fact, KnownFact)
            else None
        )
        return (None if typed is None else typed.model_dump(mode="json")), None
    if isinstance(source, ContextInput):
        selector, snapshot = source.selector, claimed.previous.snapshot()
        if (
            selector.source is not ContextSource.CONTEXT_SNAPSHOT
            or selector.schema_ref != snapshot.context_schema_ref
        ):
            return None, None
        value = next(
            (
                item
                for item in snapshot.values
                if item.field_path == selector.field_path
            ),
            None,
        )
        if value is None:
            return None, None
        raw = _plain(value.value)
        if selector.field_path == ("parking_area",):
            if (
                region is None
                or not isinstance(value.provenance, RegionDefinitionProvenance)
                or value.provenance.region_ref != snapshot.region_ref
                or raw != region.model_dump(mode="json")
            ):
                return None, None
        return raw, None

    selected = state.completions.get(source.step_id)
    verified = state.sidecars.get(source.step_id)
    if selected is None or selected.selection is None or verified is None:
        return None, None
    execution = selected.result.execution
    definition = claimed.opened.scope.resolve(execution.tool_ref, ToolDefinition)
    if source.selector.schema_ref != definition.output_schema_ref:
        return None, None
    declaration = next(
        (
            entry
            for entry in definition.fact_outputs
            if entry.value_path == source.selector.field_path
        ),
        None,
    )
    attempt = execution.attempt_result
    if declaration is None or not isinstance(attempt, ReturnedToolAttempt):
        return None, None
    result = attempt.result
    if isinstance(result, FailedToolResult) or not any(
        isinstance(item, KnownToolOutputState)
        and item.output_name == declaration.output_name
        for item in result.output_states
    ):
        return None, None
    typed = _record_value(result.payload, source.selector.field_path)
    if typed is None or not any(
        _named_asset(selected, path[0]) == verified.asset
        for path in declaration.evidence_paths or ()
    ):
        return None, None
    return typed.model_dump(mode="json"), MaskInput(
        AncestorToolEvidence(
            target, selected, verified.asset, result_evidence_slot=target[0]
        ),
        verified.content,
    )


def _completion_output(
    claimed: ClaimedNode,
    inputs: ToolNodeInput,
    plan: ToolPlanDefinition,
    state: _Round,
) -> NodeResult:
    contents = tuple(
        state.completions[step.step_id]
        for step in plan.steps
        if step.step_id in state.completions
    )
    executions = tuple(item.result.execution for item in contents)
    facts = tuple(fact for item in contents for fact in item.result.facts)
    authority = tuple(item.selection for item in contents if item.selection is not None)
    evidence: dict[str, EvidenceAsset] = {}
    for item in contents:
        for asset in item.result.evidence:
            if evidence.setdefault(asset.evidence_id, asset) != asset:
                raise AdapterRejected("geometry.conflicting_evidence_identity")
    invocations = tuple(
        invocation for item in contents for invocation in item.result.invocations
    )
    artifacts = (
        *(
            artifact(K.TOOL_EXECUTION, item.tool_execution_id, item)
            for item in executions
        ),
        *(artifact(K.FACT, item.fact_id, item) for item in facts),
        *(
            artifact(K.AUTHORITATIVE_RESULT_SELECTION, item.selection_id, item)
            for item in authority
        ),
        *(artifact(K.EVIDENCE, item.evidence_id, item) for item in evidence.values()),
        *(
            artifact(K.MODEL_INVOCATION, item.model_invocation_id, item)
            for item in invocations
        ),
    )
    status: Literal["succeeded", "partial", "failed"] = (
        "failed"
        if state.publication_blocked
        else ("partial" if authority else "failed")
        if state.issues
        else "succeeded"
    )
    summary = ToolPlanRunSummary(
        tool_plan_ref=inputs.tool_plan_ref,
        object_ref=ObjectRef(
            record_kind=K.OBJECT, record_id=inputs.object_record.object_id
        ),
        status=status,
        steps=tuple(state.outcomes[step.step_id] for step in plan.steps),
        validation_issues=tuple(state.issues),
    )
    if status == "succeeded" and not contents:
        artifacts = (tool_plan_completion_artifact(inputs, summary),)
    output = ToolNodeOutput(
        schema_version=VERSION,
        envelope=_output_envelope(
            claimed,
            () if state.publication_blocked else artifacts,
            contents,
            status,
            summary.validation_issues,
        ),
        summary=summary,
        tool_fact_refs=tuple(
            FactRef(record_kind=K.FACT, record_id=item.fact_id) for item in facts
        ),
        evidence_refs=tuple(
            EvidenceRef(record_kind=K.EVIDENCE, record_id=item.evidence_id)
            for item in evidence.values()
        ),
    )
    return NodeResult(
        ToolMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=output,
            executions=executions,
            facts=facts,
            authorities=authority,
            evidence=tuple(evidence.values()),
            nested_invocations=invocations,
            observation_input_artifacts=tuple(state.observation_input_artifacts),
        ),
        artifacts,
        tuple(
            ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=item.model_invocation_id
            )
            for item in invocations
        ),
    )


def _output_envelope(
    claimed: ClaimedNode,
    artifacts: tuple[SyntheticArtifact, ...],
    completions: tuple[StepCompletion, ...],
    summary: Literal["succeeded", "partial", "failed"],
    issues: tuple[ValidationIssue, ...],
) -> NodeOutputEnvelope:
    at = claimed.attempt.started_at
    if at is None:
        raise AdapterRejected("geometry.parent_attempt_not_started")
    finished = max(
        (
            at,
            datetime.now(UTC),
            *(item.result.execution.completed_at for item in completions),
        )
    )
    common = {
        "schema_version": VERSION,
        "node_id": NodeId.N05,
        "node_run_id": claimed.node.node_run_id,
        "attempt_id": claimed.attempt.attempt_id,
        "trace": claimed.node.trace,
        "output_artifacts": tuple(item.pin for item in artifacts),
        "started_at": at,
        "finished_at": finished,
        "duration_ms": (finished - at) // timedelta(milliseconds=1),
    }
    if summary == "failed":
        return FailedNodeOutput.model_validate(
            {
                **common,
                "status": NodeRunStatus.FAILED,
                "error": NodeError(
                    code="geometry.tool_plan_incomplete",
                    error_class="dependency",
                    retryable=False,
                    message="The ToolPlan could not complete.",
                    validation_issues=issues,
                ),
            }
        )
    return SucceededNodeOutput.model_validate(
        {**common, "status": NodeRunStatus.SUCCEEDED}
    )


@dataclass(frozen=True, slots=True)
class GeometryNodeAdapter:
    media: LocalMediaStore
    tools: ToolExecutionAdapter
    input_type: type[GeometryInput] = GeometryInput

    async def execute(self, claimed: ClaimedNode) -> NodeResult:
        opened, scope = claimed.opened, claimed.opened.scope
        env = invocation_envelope(opened, claimed.branch, claimed.node, claimed.attempt)
        if env.node_id is not NodeId.N05:
            raise AdapterRejected("geometry.unsupported_node")
        obj = claimed.previous.object_record(claimed.branch)
        profile = scope.resolve(obj.profile_ref, ProfileDefinition)
        plan_ref = profile.tool_plan_ref
        if plan_ref is None:
            raise AdapterRejected("geometry.tool_plan_not_bound")
        plan = scope.resolve(plan_ref, ToolPlanDefinition)
        observation = claimed.previous.observation
        inputs, _ = _inputs(claimed, env, plan, profile, observation)
        recovered = await self.tools.recover_selected(claimed, inputs)
        if recovered is None:
            raise AdapterRejected("geometry.parent_step_recovery_fence_lost")
        active_steps = {step.step_id: step.tool_ref for step in plan.steps}
        state = _Round(plan_ancestors(plan.steps))
        for archived in recovered:
            origin = archived.completion.result.execution.invocation_origin
            if (
                not isinstance(origin, ToolPlanStepOrigin)
                or origin.tool_plan_ref != inputs.tool_plan_ref
                or active_steps.get(origin.step_id)
                != archived.completion.result.execution.tool_ref
            ):
                raise AdapterRejected("geometry.recovered_step_outside_plan")
            state.recovered[origin.step_id] = archived
        try:
            original = await self.media.verified_task_original(
                claimed.claim.scope, scope.task
            )
        except MediaError:
            if not plan.steps:
                raise AdapterRejected("geometry.original_unavailable") from None
            state.publication_blocked = True
            for step in plan.steps:
                state.block(
                    step,
                    "geometry.original_unavailable",
                    "The exact original image could not be verified.",
                )
            return _completion_output(claimed, inputs, plan, state)
        if (
            original.asset != opened.source.media
            or original.coordinate_system != obj.coordinate_system
        ):
            if not plan.steps:
                raise AdapterRejected("geometry.original_media_mismatch")
            state.publication_blocked = True
            for step in plan.steps:
                state.block(
                    step,
                    "geometry.original_media_mismatch",
                    "The original image differs from the pinned source.",
                )
            return _completion_output(claimed, inputs, plan, state)
        if not plan.steps:
            return _completion_output(claimed, inputs, plan, state)
        region = _parking_area(claimed, original)
        steps = {
            step.step_id: (step, scope.resolve(step.tool_ref, ToolDefinition))
            for step in plan.steps
        }
        failures = {
            step.step_id: scope.resolve(
                step.failure_policy_ref, ToolFailurePolicyArtifact
            ).policy
            for step in plan.steps
        }
        fact_steps = {
            step.step_id
            for step in plan.steps
            if needs_observation(
                step,
                obj,
                scope.resolve(step.skip_policy_ref, ToolSkipPolicyArtifact).policy,
            )
        }
        legacy_fact_pins = any(
            pin.ref.record_kind is K.FACT for pin in env.upstream_artifacts
        )
        observation_settled = (
            claimed.await_observation is None or observation is not None
        )
        while len(state.outcomes) < len(steps):
            ready = tuple(
                step_id
                for step_id, (step, _) in steps.items()
                if step_id not in state.outcomes
                and set(step.depends_on) <= state.outcomes.keys()
            )
            if not ready:
                raise AdapterRejected("geometry.tool_plan_dependency_cycle")
            ready_step = ready[0]
            if not observation_settled:
                independent = next(
                    (
                        step_id
                        for step_id in ready
                        if step_id not in fact_steps
                        or not state.dependencies_available(steps[step_id][0], failures)
                    ),
                    None,
                )
                if independent is None:
                    assert claimed.await_observation is not None
                    observation = await claimed.await_observation()
                    observation_settled = True
                else:
                    ready_step = independent
            step, definition = steps[ready_step]
            child_inputs, facts = _inputs(
                claimed,
                env,
                plan,
                profile,
                observation,
                None if legacy_fact_pins else step,
            )
            await self._step(
                claimed,
                child_inputs,
                facts,
                original,
                region,
                state,
                step,
                definition,
                failures,
            )
        return _completion_output(claimed, inputs, plan, state)

    async def _step(
        self,
        claimed: ClaimedNode,
        inputs: ToolNodeInput,
        facts: tuple[FactRecord, ...],
        original: VerifiedOriginal,
        region: ParkingArea | None,
        state: _Round,
        step: ToolStep,
        definition: ToolDefinition,
        failures: dict[str, ToolFailurePolicy],
    ) -> None:
        scope = claimed.opened.scope
        archived = state.recovered.get(step.step_id)
        if archived is not None and archived.audit_only:
            state.block(
                step,
                "geometry.legacy_ancestor_proof_unavailable",
                "The archived result does not contain the required source proof.",
            )
            return
        slot = _OUTPUT_SLOTS.get(definition.tool_id)
        if slot is None:
            state.block(
                step,
                "geometry.tool_unavailable",
                "No approved local CPU adapter is registered for this Tool.",
            )
            return
        skip = scope.resolve(step.skip_policy_ref, ToolSkipPolicyArtifact).policy
        if failures[step.step_id].requirement != step.requirement:
            state.block(
                step,
                "geometry.step_policy_unresolved",
                "The configured step policy could not be evaluated.",
            )
            return
        if not state.dependencies_available(step, failures):
            state.block(
                step,
                "geometry.dependency_unavailable",
                "A configured ancestor has no authoritative result.",
            )
            return
        state.observation_input_artifacts.update(
            dict.fromkeys(
                inputs.envelope.upstream_artifacts[len(claimed.node.upstream_inputs) :]
            )
        )
        guard = _step_guard(claimed, inputs, facts, step, skip, state)
        if guard is RuleTruth.UNKNOWN:
            state.block(
                step,
                "geometry.step_guard_unknown",
                "The configured step guard is unresolved.",
            )
            return
        if guard is RuleTruth.FALSE:
            if archived is not None:
                state.block(
                    step,
                    "geometry.step_guard_changed",
                    "The guard disagrees with the archived step.",
                )
            else:
                state.outcomes[step.step_id] = UninvokedToolStep(
                    disposition="skipped",
                    step_id=step.step_id,
                    reason_code="geometry.step_guard_false",
                    validation_issues=(),
                )
            return
        fields: dict[str, object] = {}
        proofs: list[MaskInput] = []
        for binding in step.input_bindings:
            target = binding.target.field_path
            if (
                len(target) != 1
                or binding.target.schema_ref.stable_json()
                != definition.input_schema_ref.stable_json()
            ):
                state.block(
                    step,
                    "geometry.input_binding_unsupported",
                    "The exact tool input binding is unsupported.",
                )
                return
            value, proof = _source_field(
                binding.source,
                inputs,
                claimed,
                facts,
                original,
                region,
                state,
                target,
            )
            if value is None:
                if binding.required or binding.on_missing == "block":
                    state.block(
                        step,
                        "geometry.step_input_unavailable",
                        "A required configured input is unavailable.",
                    )
                    return
                continue
            fields[target[0]] = value
            if proof is not None:
                proofs.append(proof)
        policy_ref = PolicyContentRef.model_validate(
            definition.evidence_policy_ref.model_dump()
        )
        policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
        retention = RetentionPolicyRef(
            evidence_policy_ref=policy_ref, retention_class=policy.retention_class
        )

        async def stage(
            dispatch: ToolDispatch, sidecar: GeometrySidecar
        ) -> EvidenceAsset:
            return await _stage(
                self.media, claimed.claim.scope, dispatch, sidecar, retention, policy
            )

        prepared = self.input_type(
            original.asset,
            _original_evidence(claimed, original, policy_ref, policy),
            stage,
            object_bbox=inputs.object_record.bbox,
            parking_area=region,
            masks=tuple(proofs),
            member_boxes=inputs.object_record.grounding_member_boxes
            or (inputs.object_record.bbox,),
        )
        finished = await self.tools.execute_step(
            claimed,
            inputs,
            step.step_id,
            fields,
            ancestor_evidence=prepared.ancestor_evidence,
            cpu_exchange=prepared.exchange(),
        )
        state.record(step, finished)
        execution = finished.result.execution
        if finished.selection is None:
            state.issues.append(
                _issue(
                    step.step_id,
                    "geometry.step_not_selected",
                    "The child Attempt did not select an authoritative result.",
                )
            )
            return
        asset = _named_asset(finished, slot)
        try:
            if asset is None:
                raise MediaError("tool_sidecar_output_unavailable")
            state.sidecars[step.step_id] = await self.media.read_tool_sidecar(
                claimed.claim.scope,
                execution.execution_id,
                execution.tool_execution_id,
                slot,
                expected_asset=asset,
            )
        except MediaError:
            state.publication_blocked = True
            state.issues.append(
                _issue(
                    step.step_id,
                    "geometry.sidecar_unavailable",
                    "The selected child sidecar could not be verified.",
                )
            )
