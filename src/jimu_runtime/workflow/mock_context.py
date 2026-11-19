"""N01 and Workflow-owned eligibility for the offline parking fixture."""

from dataclasses import dataclass
from datetime import datetime

from jimu_runtime.domain.conditions import LiteralExpr
from jimu_runtime.domain.definition_enums import ContextProvenanceSource
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus
from jimu_runtime.domain.nodes.context import (
    ContextBuildInput,
    ContextBuildOutput,
    FailedContextBuild,
    RequiredContextFailureReport,
    SuccessfulContextBuild,
    UsableContextReport,
)
from jimu_runtime.domain.policies import (
    ApplicabilityPolicyArtifact,
    ContextProvenancePolicyArtifact,
    ExecutionPolicyArtifact,
)
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityDisposition,
    ApplicabilityEvaluationOwner,
    ApplicabilityEvaluationRef,
    ApplicabilityEvaluationStatus,
    ApplicabilityLevel,
    ApplicabilityReason,
    ApplicabilityTargetRef,
    ApplicabilityTrace,
    ContextSnapshotRef,
    EvaluatedApplicabilityLevel,
    ProfileEligibilityEvaluation,
    ProfileEligibilityEvaluationRef,
    RuleTruth,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    NodeRunRef,
    PolicyContentRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.intake import (
    ContextSnapshot,
    ContextValue,
    TaskMetadataProvenance,
)
from jimu_runtime.domain.schema_types import FieldSelector
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityRecordSet,
    ApplicabilityScope,
    ApplicabilityTargetBinding,
    validate_applicability_records,
)
from jimu_runtime.domain.validation.authority_inputs import NodeRunDescriptor
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.validation.schema_descriptors import resolve_field
from jimu_runtime.domain.values import (
    BooleanValue,
    RecordValue,
    Value,
    validate_typed_value,
)
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticArtifact,
    VerifiedExecutionScope,
    artifact,
    record_digest,
    synthetic_id,
    synthetic_issue,
)


@dataclass(frozen=True, slots=True)
class SyntheticContextResult:
    output: ContextBuildOutput
    snapshot: ContextSnapshot | None
    artifacts: tuple[SyntheticArtifact, ...]


@dataclass(frozen=True, slots=True)
class SyntheticGateResult:
    evaluation: ProfileEligibilityEvaluation
    traces: tuple[ApplicabilityTrace, ...]
    scope: ApplicabilityScope

    @property
    def artifacts(self) -> tuple[SyntheticArtifact, ...]:
        return (
            artifact(
                K.PROFILE_ELIGIBILITY_EVALUATION,
                self.evaluation.evaluation_id,
                self.evaluation,
            ),
            *(
                artifact(K.APPLICABILITY_TRACE, trace.applicability_trace_id, trace)
                for trace in self.traces
            ),
        )


def _metadata_value(metadata: RecordValue, path: tuple[str, ...]) -> Value | None:
    value: Value | None = metadata
    for part in path:
        if not isinstance(value, RecordValue):
            return None
        value = value.value.get(part)
    return value


def build_context(
    scope: VerifiedExecutionScope, inputs: ContextBuildInput, at: datetime
) -> SyntheticContextResult:
    inputs = ContextBuildInput.model_validate(inputs)
    call = scope.check_call(inputs.envelope, NodeId.N01, at)
    if inputs.accepted_task != scope.task or inputs.media_asset != scope.media:
        raise ValueError(
            "context must consume accepted Task and finalized media from this scope"
        )
    call.check_upstream(artifact(K.TASK, scope.task.task_id, scope.task))
    region = scope.resolve(inputs.region_ref, RegionDefinition)
    config = scope.resolve(scope.execution_config_ref, ExecutionPolicyArtifact).policy
    if reference_identity(inputs.context_schema_ref) != reference_identity(
        region.context_schema_ref
    ) or reference_identity(inputs.context_provenance_policy_ref) != reference_identity(
        config.context_provenance_policy_ref
    ):
        raise ValueError(
            "N01 must consume the pinned Region and context provenance configuration"
        )
    policy = scope.resolve(
        inputs.context_provenance_policy_ref, ContextProvenancePolicyArtifact
    ).policy
    values: list[ContextValue] = []
    required: list[ValidationIssue] = []
    optional: list[ValidationIssue] = []
    for entry in policy.fields:
        if (
            entry.schema_ref != inputs.context_schema_ref
            or entry.allowed_sources != (ContextProvenanceSource.TRUSTED_TASK_METADATA,)
            or entry.time_sensitive
            or not entry.field_path
        ):
            raise ValueError("fixture N01 supports declared trusted Task metadata only")
        selected = _metadata_value(scope.task.context.metadata, entry.field_path)
        field = resolve_field(
            FieldSelector(schema_ref=entry.schema_ref, field_path=entry.field_path),
            scope.schema_descriptors,
        )
        try:
            value = validate_typed_value(field.type_definition, selected)
        except ValueError:
            output_issues = required if entry.required else optional
            output_issues.append(
                synthetic_issue(
                    "synthetic-context-missing"
                    if selected is None
                    else "synthetic-context-invalid",
                    "Required or optional synthetic context did not match its declared schema",
                    entry.field_path,
                )
            )
        else:
            values.append(
                ContextValue(
                    field_path=entry.field_path,
                    value=value,
                    provenance=TaskMetadataProvenance(
                        source=ContextProvenanceSource.TRUSTED_TASK_METADATA,
                        task_id=scope.task.task_id,
                    ),
                )
            )
    if required:
        failed = FailedContextBuild(
            schema_version=VERSION,
            envelope=call.output((), required[0]),
            disposition="required_context_failure",
            validation_report=RequiredContextFailureReport(
                disposition="required_context_failure",
                required_issues=tuple(required),
                optional_issues=tuple(optional),
            ),
        )
        failed.check_invocation(inputs)
        return SyntheticContextResult(failed, None, ())
    snapshot = ContextSnapshot(
        schema_version=VERSION,
        context_snapshot_id=call.id("context"),
        execution_id=scope.execution_id,
        task_id=scope.task.task_id,
        release_id=scope.pins.release_id,
        bundle_digest=scope.pins.bundle_digest,
        media_ref=scope.media.media_ref,
        device_ref=inputs.device_ref,
        camera_ref=inputs.camera_ref,
        region_ref=inputs.region_ref,
        context_schema_ref=inputs.context_schema_ref,
        context_provenance_policy_ref=inputs.context_provenance_policy_ref,
        clock=scope.task.context.clock,
        values=tuple(values),
        created_at=at,
    )
    item = snapshot_artifact(snapshot)
    output = SuccessfulContextBuild(
        schema_version=VERSION,
        envelope=call.output((item,)),
        disposition="usable",
        validation_report=UsableContextReport(
            disposition="usable", optional_issues=tuple(optional)
        ),
        context_snapshot_ref=snapshot_ref(snapshot),
        context_snapshot_digest=item.pin.digest,
    )
    output.check_snapshot(inputs, snapshot)
    return SyntheticContextResult(output, snapshot, (item,))


def snapshot_ref(snapshot: ContextSnapshot) -> ContextSnapshotRef:
    return ContextSnapshotRef(
        record_kind=K.CONTEXT_SNAPSHOT, record_id=snapshot.context_snapshot_id
    )


def snapshot_artifact(snapshot: ContextSnapshot) -> SyntheticArtifact:
    return artifact(K.CONTEXT_SNAPSHOT, snapshot.context_snapshot_id, snapshot)


def literal_applicability(
    scope: VerifiedExecutionScope,
    policy_ref: PolicyContentRef,
    target: ApplicabilityTargetRef,
    level: ApplicabilityLevel,
    evaluation_ref: ApplicabilityEvaluationRef,
) -> EvaluatedApplicabilityLevel:
    policy = scope.resolve(policy_ref, ApplicabilityPolicyArtifact).policy
    expr = policy.condition_ast
    if (
        policy.identity_constraints
        or policy.context_allowlist
        or not isinstance(expr, LiteralExpr)
        or not isinstance(expr.constant.value, BooleanValue)
    ):
        raise ValueError(
            "fixture applicability only supports declared boolean literal policies"
        )
    included = expr.constant.value.value
    return EvaluatedApplicabilityLevel(
        level=level,
        target_ref=target,
        policy_ref=policy_ref,
        evaluation_status=ApplicabilityEvaluationStatus.EVALUATED,
        truth=RuleTruth.TRUE if included else RuleTruth.FALSE,
        evaluation_owner=(
            ApplicabilityEvaluationOwner.PRE_N02_GATE
            if level is ApplicabilityLevel.PROFILE
            else ApplicabilityEvaluationOwner.N06
        ),
        source_evaluation_ref=evaluation_ref,
        declared_input_refs=(),
        reason_code=ApplicabilityReason.INCLUDED_BY_POLICY
        if included
        else ApplicabilityReason.EXCLUDED_BY_POLICY,
        reason_evidence_refs=() if included else (policy_ref,),
        validation_issues=(),
    )


def applicability_binding(
    scope: VerifiedExecutionScope,
    target: ApplicabilityTargetRef,
    policy_ref: PolicyContentRef,
    children: tuple[ApplicabilityTargetRef, ...] = (),
) -> ApplicabilityTargetBinding:
    return ApplicabilityTargetBinding(
        target_ref=target,
        policy_ref=policy_ref,
        policy=scope.resolve(policy_ref, ApplicabilityPolicyArtifact),
        child_refs=children,
    )


def _configured_profiles(
    scope: VerifiedExecutionScope,
) -> tuple[ProfileContentRef, ...]:
    return tuple(
        ProfileContentRef.model_validate(entry.ref.model_dump())
        for entry in scope.catalogue.of_kind(ArtifactKind.PROFILE)
    )


def check_gate_scope(
    scope: VerifiedExecutionScope, gate: ProfileEligibilityEvaluation
) -> None:
    ProfileEligibilityEvaluation.model_validate(gate)
    if (
        gate.execution_id,
        gate.release_id,
        gate.bundle_digest,
        gate.workflow_version,
        gate.configured_profile_refs,
    ) != (
        scope.execution_id,
        scope.pins.release_id,
        scope.pins.bundle_digest,
        scope.pins.workflow_version,
        _configured_profiles(scope),
    ):
        raise ValueError(
            "synthetic Profile gate must exhaust the exact configured execution scope"
        )


def build_gate(
    scope: VerifiedExecutionScope,
    context_input: ContextBuildInput,
    output: SuccessfulContextBuild,
    snapshot: ContextSnapshot,
    at: datetime,
) -> SyntheticGateResult:
    scope.check_call(context_input.envelope, NodeId.N01, output.envelope.started_at)
    output.check_snapshot(context_input, snapshot)
    if (
        context_input.accepted_task != scope.task
        or context_input.media_asset != scope.media
        or output.context_snapshot_digest != record_digest(snapshot)
        or at < output.envelope.finished_at
    ):
        raise ValueError("pre-N02 requires the selected successful N01 output")
    refs = _configured_profiles(scope)
    gate_id = synthetic_id(
        "eligibility",
        scope.task.task_id,
        scope.execution_id,
        snapshot.context_snapshot_id,
    )
    gate_ref = ProfileEligibilityEvaluationRef(
        record_kind=K.PROFILE_ELIGIBILITY_EVALUATION, record_id=gate_id
    )
    pin = {
        "schema_version": VERSION,
        "execution_id": scope.execution_id,
        "release_id": scope.pins.release_id,
        "bundle_digest": scope.pins.bundle_digest,
        "context_snapshot_ref": snapshot_ref(snapshot),
    }
    traces: list[ApplicabilityTrace] = []
    bindings: list[ApplicabilityTargetBinding] = []
    for ref in refs:
        profile = scope.resolve(ref, ProfileDefinition)
        policy_ref = PolicyContentRef.model_validate(
            profile.applicability_policy_ref.model_dump()
        )
        bindings.append(
            applicability_binding(scope, ref, policy_ref, profile.rule_set_refs)
        )
        level = literal_applicability(
            scope, policy_ref, ref, ApplicabilityLevel.PROFILE, gate_ref
        )
        traces.append(
            ApplicabilityTrace.model_validate(
                {
                    **pin,
                    "applicability_trace_id": synthetic_id(
                        "profile-trace", gate_id, ref.stable_json()
                    ),
                    "profile_ref": ref,
                    "target_ref": ref,
                    "context_snapshot_digest": record_digest(snapshot),
                    "levels": (level,),
                    "disposition": ApplicabilityDisposition.INCLUDED
                    if level.truth is RuleTruth.TRUE
                    else ApplicabilityDisposition.EXCLUDED,
                    "effective_truth": level.truth,
                    "decisive_level": level.level,
                    "created_at": at,
                }
            )
        )
    gate = ProfileEligibilityEvaluation.model_validate(
        {
            **pin,
            "evaluation_id": gate_id,
            "workflow_version": scope.pins.workflow_version,
            "evaluated_at": at,
            "configured_profile_refs": refs,
            "eligible_profiles": tuple(
                trace.profile_ref
                for trace in traces
                if trace.effective_truth is RuleTruth.TRUE
            ),
            "excluded_profiles": tuple(
                trace.profile_ref
                for trace in traces
                if trace.effective_truth is RuleTruth.FALSE
            ),
            "unresolved_profiles": (),
            "applicability_traces": tuple(trace.trace_ref for trace in traces),
        }
    )
    gate_scope = ApplicabilityScope(
        execution_id=scope.execution_id,
        release_id=scope.pins.release_id,
        bundle_digest=scope.pins.bundle_digest,
        workflow_version=scope.pins.workflow_version,
        context_snapshot_ref=snapshot_ref(snapshot),
        context_snapshot_digest=record_digest(snapshot),
        context_node_run=NodeRunDescriptor(
            node_run_ref=NodeRunRef(
                record_kind=K.NODE_RUN, record_id=output.envelope.node_run_id
            ),
            execution_id=scope.execution_id,
            node_id="N01",
            status=NodeRunStatus.SUCCEEDED,
        ),
        configured_profile_refs=refs,
        target_bindings=tuple(bindings),
    )
    validate_applicability_records(
        gate_scope,
        ApplicabilityRecordSet(
            profile_eligibility=gate,
            traces=tuple(traces),
            rule_set_evaluations=(),
            rule_evaluations=(),
            unresolved_obligations=(),
            node_runs=(),
            attempts=(),
            objects=(),
        ),
    )
    return SyntheticGateResult(gate, tuple(traces), gate_scope)
