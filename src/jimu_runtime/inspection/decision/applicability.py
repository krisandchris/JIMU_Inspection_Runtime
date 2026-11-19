"""Evaluate the ordered applicability gates using pinned metadata."""

from dataclasses import dataclass
from datetime import datetime

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definition_enums import ContextSource
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus
from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.nodes.context import ContextBuildInput, SuccessfulContextBuild
from jimu_runtime.domain.policies import ApplicabilityPolicyArtifact
from jimu_runtime.domain.producers import ProducerCatalogue
from jimu_runtime.domain.rules import RuleSetContentRef
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityDiagnosticRef,
    ApplicabilityDisposition,
    ApplicabilityEvaluationOwner,
    ApplicabilityEvaluationRef,
    ApplicabilityEvaluationStatus,
    ApplicabilityLevel,
    ApplicabilityLevelTrace,
    ApplicabilityReason,
    ApplicabilityTargetRef,
    ApplicabilityTrace,
    EvaluatedApplicabilityLevel,
    NotEvaluatedApplicabilityLevel,
    ProfileEligibilityEvaluation,
    ProfileEligibilityEvaluationRef,
    RuleSetApplicabilityEvaluationRef,
    RuleTruth,
    UnresolvedObligation,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    NodeRunRef,
    ObjectRef,
    PolicyContentRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.intake import (
    ContextSnapshot,
    RegionDefinitionProvenance,
)
from jimu_runtime.domain.schema_types import FieldSelector
from jimu_runtime.domain.validation.applicability import (
    ApplicabilityRecordSet,
    ApplicabilityScope,
    ApplicabilityTargetBinding,
    validate_applicability_records,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
    NodeRunDescriptor,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ApplicabilityConditionContext,
)
from jimu_runtime.domain.validation.definition_inputs import (
    ProfileRegistration,
    ProfileUseContext,
    QuestionRegistration,
    RegisteredTool,
    ToolPlanRegistration,
)
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.validation.producer_inputs import ProducerCatalogueRegistration
from jimu_runtime.domain.validation.rule_type_inputs import RuleTypeBindings
from jimu_runtime.domain.validation.schema_descriptors import resolve_field
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import RecordValue, Value, validate_typed_value
from jimu_runtime.inspection.perception.context import (
    _typed_raw,
    validate_context_projection,
)
from jimu_runtime.inspection.rules.predicates import PreparedPredicate
from jimu_runtime.workflow.mock_support import VERSION, K, record_digest, synthetic_id
from jimu_runtime.workflow.worker_records import EligibilityMessage
from jimu_runtime.workflow.worker_source import OpenExecution

_DISPOSITION = {
    RuleTruth.TRUE: ApplicabilityDisposition.INCLUDED,
    RuleTruth.FALSE: ApplicabilityDisposition.EXCLUDED,
    RuleTruth.UNKNOWN: ApplicabilityDisposition.UNRESOLVED,
}


def type_bindings(opened: OpenExecution) -> RuleTypeBindings:
    definitions = opened.resolved.definition_inputs
    (producer,) = opened.scope.catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    producer_ref = ProducerCatalogueContentRef.model_validate(producer.ref.model_dump())
    return RuleTypeBindings(
        profiles=tuple(
            item
            for item in definitions.definitions
            if isinstance(item, ProfileRegistration)
        ),
        questions=tuple(
            AuthorityQuestionInput(ref=item.ref, definition=item.definition)
            for item in definitions.definitions
            if isinstance(item, QuestionRegistration)
        ),
        plans=tuple(
            AuthorityToolPlanInput(ref=item.ref, definition=item.definition)
            for item in definitions.definitions
            if isinstance(item, ToolPlanRegistration)
        ),
        tools=tuple(
            ToolRegistration(ref=item.ref, definition=item.definition)
            for item in definitions.definitions
            if isinstance(item, RegisteredTool)
        ),
        policies=definitions.policies,
        producer_catalogue=ProducerCatalogueRegistration(
            ref=producer_ref,
            definition=opened.scope.resolve(producer_ref, ProducerCatalogue),
        ),
    )


@dataclass(frozen=True, slots=True)
class MetadataInputs:
    context: ContextSnapshot
    output: SuccessfulContextBuild

    def projected_value(self, selector: ContextFieldSelector) -> Value | None:
        for item in self.context.values:
            if selector.source is ContextSource.REGION_METADATA and not isinstance(
                item.provenance, RegionDefinitionProvenance
            ):
                continue
            if selector.field_path[: len(item.field_path)] != item.field_path:
                continue
            value: Value | None = item.value
            for part in selector.field_path[len(item.field_path) :]:
                value = (
                    value.value.get(part) if isinstance(value, RecordValue) else None
                )
            return value
        return None

    def missing_diagnostics(
        self,
        selectors: tuple[ContextFieldSelector, ...],
        fields: dict[ContextFieldSelector, Value],
    ) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue
            for issue in self.output.validation_report.optional_issues
            if any(
                selector.source is ContextSource.CONTEXT_SNAPSHOT
                and selector not in fields
                and issue.path == selector.field_path[: len(issue.path)]
                for selector in selectors
            )
        )


@dataclass(frozen=True, slots=True)
class ApplicabilityEvaluator:
    opened: OpenExecution
    metadata: MetadataInputs
    bindings: RuleTypeBindings

    def use(self, profile: ProfileContentRef) -> ProfileUseContext:
        uses = tuple(
            item
            for item in self.opened.resolved.definition_inputs.use_contexts
            if reference_identity(item.profile_ref) == reference_identity(profile)
            and item.region_ref in (None, self.metadata.context.region_ref)
        )
        exact = tuple(
            item for item in uses if item.region_ref == self.metadata.context.region_ref
        )
        (use,) = exact or uses
        return use

    def fields(
        self,
        profile: ProfileContentRef,
        selectors: tuple[ContextFieldSelector, ...],
    ) -> dict[ContextFieldSelector, Value]:
        fields = {}
        use = self.use(profile)
        profile_definition = self.opened.scope.resolve(profile, ProfileDefinition)
        for selector in selectors:
            source: ContractModel
            if not any(
                registration.source == selector.source.value
                and registration.schema_ref == selector.schema_ref
                for registration in use.context_registrations
            ):
                continue
            if selector.source is ContextSource.CONTEXT_SNAPSHOT:
                projected = self.metadata.projected_value(selector)
                if projected is not None:
                    fields[selector] = projected
                continue
            if selector.source is ContextSource.REGION_METADATA:
                projected = self.metadata.projected_value(selector)
                if projected is not None:
                    fields[selector] = projected
                    continue
                source = self.opened.region
            else:
                source = profile_definition

            raw: object = source.model_dump(mode="json")
            for part in selector.field_path:
                raw = raw.get(part) if isinstance(raw, dict) else None
            if raw is None:
                continue
            shape = resolve_field(
                FieldSelector(
                    schema_ref=selector.schema_ref, field_path=selector.field_path
                ),
                self.opened.resolved.schema_descriptors,
            ).type_definition
            try:
                fields[selector] = validate_typed_value(shape, _typed_raw(shape, raw))
            except ValueError:
                # A shape without compatible source data is unavailable; only
                # exact declared values map into Rule facts.
                continue
        return fields

    def evaluate(
        self,
        profile: ProfileContentRef,
        target: ApplicabilityTargetRef,
        policy_ref: PolicyContentRef,
        level: ApplicabilityLevel,
        source: ApplicabilityEvaluationRef,
    ) -> EvaluatedApplicabilityLevel:
        scope = self.opened.scope
        policy = scope.resolve(policy_ref, ApplicabilityPolicyArtifact).policy
        declared: tuple[ContextFieldSelector, ...] = ()
        diagnostics: tuple[ValidationIssue, ...] = ()
        evidence: tuple[PolicyContentRef | ContextFieldSelector, ...] = ()
        for ref in (profile, self.metadata.context.region_ref):
            constraints = tuple(
                item
                for item in policy.identity_constraints
                if item.artifact_kind is ref.artifact_kind
            )
            if constraints and all(
                reference_identity(scope.catalogue.resolve_selector(item).ref)
                != reference_identity(ref)
                for item in constraints
            ):
                truth = RuleTruth.FALSE
                evidence = (policy_ref,)
                reason = ApplicabilityReason.EXCLUDED_BY_POLICY
                break
        else:
            use = self.use(profile)
            declared = tuple(
                dict.fromkeys(
                    condition_references(policy.condition_ast).context_selectors
                )
            )
            fields = self.fields(profile, declared)
            prepared = PreparedPredicate(
                catalogue=scope.catalogue,
                input_domain=policy.input_domain,
                context=ApplicabilityConditionContext.model_validate(
                    {
                        "use_role": "applicability",
                        "profile_ref": profile.model_dump(),
                        "policy_ref": policy_ref.model_dump(),
                        "context_registrations": use.context_registrations,
                        "descriptors": self.opened.resolved.schema_descriptors,
                        **(
                            {"region_ref": use.region_ref}
                            if use.region_ref is not None
                            else {}
                        ),
                    }
                ),
                bindings=self.bindings,
                fact_schemas=self.opened.resolved.fact_schemas,
            )
            evaluation = prepared.evaluate(
                execution_id=scope.execution_id, context_fields=fields
            )
            truth = evaluation.truth
            source_issues = self.metadata.missing_diagnostics(declared, fields)
            diagnostics = tuple(
                dict.fromkeys(
                    ValidationIssue(
                        code=code,
                        path=path,
                        message="Declared applicability metadata is unavailable or invalid.",
                        related_refs=(policy_ref,),
                    )
                    for code, path in (
                        *((code, ()) for code in evaluation.diagnostic_codes),
                        *((issue.code, issue.path) for issue in source_issues),
                    )
                )
            )[:64]
            if truth is RuleTruth.TRUE:
                reason = ApplicabilityReason.INCLUDED_BY_POLICY
            elif truth is RuleTruth.FALSE:
                reason = ApplicabilityReason.EXCLUDED_BY_POLICY
                evidence = (policy_ref, *fields)
            else:
                reason = (
                    ApplicabilityReason.METADATA_INVALID
                    if any("invalid" in issue.code for issue in diagnostics)
                    else ApplicabilityReason.METADATA_UNKNOWN
                )
        return EvaluatedApplicabilityLevel(
            level=level,
            target_ref=target,
            policy_ref=policy_ref,
            evaluation_status=ApplicabilityEvaluationStatus.EVALUATED,
            evaluation_owner=ApplicabilityEvaluationOwner.PRE_N02_GATE
            if level is ApplicabilityLevel.PROFILE
            else ApplicabilityEvaluationOwner.N06,
            source_evaluation_ref=source,
            truth=truth,
            declared_input_refs=declared,
            reason_code=reason,
            reason_evidence_refs=evidence,
            validation_issues=diagnostics,
        )


def extend_trace(
    parent: ApplicabilityTrace,
    level: ApplicabilityLevelTrace,
    trace_id: str,
    at: datetime,
) -> ApplicabilityTrace:
    truth, decisive = (
        (level.truth, level.level)
        if isinstance(level, EvaluatedApplicabilityLevel)
        else (parent.effective_truth, parent.decisive_level)
    )
    return ApplicabilityTrace.model_validate(
        {
            **parent.model_dump(),
            "applicability_trace_id": trace_id,
            "target_ref": level.target_ref,
            "levels": (*parent.levels, level),
            "effective_truth": truth,
            "disposition": _DISPOSITION[truth],
            "decisive_level": decisive,
            "created_at": at,
        }
    )


def project_rule(
    parent: ApplicabilityTrace,
    target: ApplicabilityTargetRef,
    policy: PolicyContentRef,
    ancestor: RuleSetApplicabilityEvaluationRef,
) -> NotEvaluatedApplicabilityLevel:
    return NotEvaluatedApplicabilityLevel(
        level=ApplicabilityLevel.RULE,
        target_ref=target,
        policy_ref=policy,
        evaluation_status=ApplicabilityEvaluationStatus.NOT_EVALUATED,
        reason_code=(
            ApplicabilityReason.ANCESTOR_EXCLUDED
            if parent.effective_truth is RuleTruth.FALSE
            else ApplicabilityReason.ANCESTOR_UNRESOLVED
        ),
        decisive_ancestor_ref=ancestor,
    )


def unresolved_gate(
    trace: ApplicabilityTrace,
    obligation_id: str,
    object_ref: ObjectRef | None = None,
    rule_set_ref: RuleSetContentRef | None = None,
) -> UnresolvedObligation:
    level = trace.levels[-1]
    if (
        not isinstance(level, EvaluatedApplicabilityLevel)
        or level.truth is not RuleTruth.UNKNOWN
    ):
        raise ValueError("unresolved obligation requires its reached UNKNOWN gate")
    return UnresolvedObligation.model_validate(
        {
            "schema_version": VERSION,
            "obligation_id": obligation_id,
            "execution_id": trace.execution_id,
            "profile_ref": trace.profile_ref,
            "scope": level.level,
            "target_ref": trace.target_ref,
            "applicability_trace_ref": trace.trace_ref,
            "reason_code": level.reason_code,
            "diagnostic_refs": tuple(
                ApplicabilityDiagnosticRef(
                    trace_ref=trace.trace_ref, level=level.level, issue_index=index
                )
                for index in range(len(level.validation_issues))
            ),
            **({"object_ref": object_ref} if object_ref is not None else {}),
            **({"rule_set_ref": rule_set_ref} if rule_set_ref is not None else {}),
        }
    )


def build_profile_gate(
    opened: OpenExecution,
    inputs: ContextBuildInput,
    output: SuccessfulContextBuild,
    snapshot: ContextSnapshot,
) -> EligibilityMessage:
    """Partition the entire pinned Profile catalogue after a successful N01."""
    scope = opened.scope
    scope.check_call(inputs.envelope, NodeId.N01, output.envelope.started_at)
    output.check_snapshot(inputs, snapshot)
    scope.check_snapshot(snapshot, output.context_snapshot_ref)
    digest = record_digest(snapshot)
    if (
        inputs.accepted_task != scope.task
        or inputs.media_asset != scope.media
        or inputs.region_ref != opened.region_ref
        or (inputs.device_ref, inputs.camera_ref)
        != (opened.source.device, opened.source.camera)
        or output.context_snapshot_digest != digest
    ):
        raise ValueError("Profile gate requires the selected successful N01 Snapshot")

    validate_context_projection(opened, inputs, output, snapshot)
    at = output.envelope.finished_at
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
        "context_snapshot_ref": output.context_snapshot_ref,
    }
    refs = tuple(plan.rules.profile_ref for plan in opened.profiles)
    configured = tuple(
        ProfileContentRef.model_validate(entry.ref.model_dump())
        for entry in scope.catalogue.of_kind(ArtifactKind.PROFILE)
    )
    if refs != configured:
        raise ValueError("Profile gate must exhaust the pinned Profile catalogue")
    bindings: list[ApplicabilityTargetBinding] = []
    traces: list[ApplicabilityTrace] = []
    evaluator = ApplicabilityEvaluator(
        opened, MetadataInputs(snapshot, output), type_bindings(opened)
    )
    for plan in opened.profiles:
        ref = plan.rules.profile_ref
        definition = scope.resolve(ref, type(plan.definition))
        policy_ref = PolicyContentRef.model_validate(
            definition.applicability_policy_ref.model_dump()
        )
        bindings.append(
            ApplicabilityTargetBinding(
                target_ref=ref,
                policy_ref=policy_ref,
                policy=scope.resolve(policy_ref, ApplicabilityPolicyArtifact),
                child_refs=definition.rule_set_refs,
            )
        )
        level = evaluator.evaluate(
            ref, ref, policy_ref, ApplicabilityLevel.PROFILE, gate_ref
        )
        traces.append(
            ApplicabilityTrace.model_validate(
                {
                    **pin,
                    "applicability_trace_id": synthetic_id(
                        "profile-trace", gate_id, ref.stable_json()
                    ),
                    "target_ref": ref,
                    "profile_ref": ref,
                    "context_snapshot_digest": digest,
                    "levels": (level,),
                    "effective_truth": level.truth,
                    "disposition": _DISPOSITION[level.truth],
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
            "unresolved_profiles": tuple(
                trace.profile_ref
                for trace in traces
                if trace.effective_truth is RuleTruth.UNKNOWN
            ),
            "applicability_traces": tuple(trace.trace_ref for trace in traces),
        }
    )
    gate_scope = ApplicabilityScope(
        execution_id=scope.execution_id,
        release_id=scope.pins.release_id,
        bundle_digest=scope.pins.bundle_digest,
        workflow_version=scope.pins.workflow_version,
        context_snapshot_ref=output.context_snapshot_ref,
        context_snapshot_digest=digest,
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
    unresolved = tuple(
        unresolved_gate(
            trace,
            synthetic_id(
                "profile-obligation",
                scope.task.task_id,
                scope.execution_id,
                trace.profile_ref.stable_json(),
            ),
        )
        for trace in traces
        if trace.effective_truth is RuleTruth.UNKNOWN
    )
    validate_applicability_records(
        gate_scope,
        ApplicabilityRecordSet(
            profile_eligibility=gate,
            traces=tuple(traces),
            rule_set_evaluations=(),
            rule_evaluations=(),
            unresolved_obligations=tuple(unresolved),
            node_runs=(),
            attempts=(),
            objects=(),
        ),
    )
    return EligibilityMessage(
        schema_version=VERSION, evaluation=gate, traces=tuple(traces), scope=gate_scope
    )
