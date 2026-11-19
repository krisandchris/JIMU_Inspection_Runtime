"""Synthetic N06 full-scope scan, staged until execution Coverage is available."""

from dataclasses import dataclass
from datetime import datetime

from jimu_runtime.domain.conditions import AllExpr, FactRefExpr
from jimu_runtime.domain.definition_enums import (
    EvidenceType,
    FactScope,
    ModelCapability,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.enums import DecisionResult
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    QuestionProducerSelector,
    ToolProducerSelector,
)
from jimu_runtime.domain.nodes.decision import DecisionInput, DecisionOutput
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.policies import (
    ConflictPolicyArtifact,
    EvidencePolicyArtifact,
    RulePolicyArtifact,
)
from jimu_runtime.domain.producers import QuestionProducer, ToolStepProducer
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityDisposition,
    ApplicabilityLevel,
    ApplicabilityTrace,
    RuleApplicabilityEvaluation,
    RuleApplicabilityEvaluationRef,
    RuleSetApplicabilityEvaluation,
    RuleSetApplicabilityEvaluationRef,
    RuleTruth,
)
from jimu_runtime.domain.runtime.authority import (
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.coverage import (
    EvaluatedObligation,
    ExcludedObligation,
    ExecutionCoverage,
    GuardBlockedObligation,
    IncludedObligation,
    MandatoryRuleObligation,
    RuleObligationTarget,
    RuleSetObligationTarget,
    SelectedApplicability,
)
from jimu_runtime.domain.runtime.decisions import (
    BlockedRequiredEvidenceGuard,
    BlockedRuleOutcome,
    CandidatePrediction,
    CandidatePredictionStatus,
    CompleteRequiredEvidenceGuard,
    ConflictResolutionTrace,
    DecisionRecord,
    DecisionRef,
    EvaluatedRuleOutcome,
    ExecutionCoverageRef,
    ExplanationTrace,
    PredicateRecord,
    RequiredEvidenceReason,
    RequiredFactProof,
    RuleEvaluation,
    RulePath,
    RuleSkipReason,
    SkippedRuleOutcome,
)
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    ObjectEvidenceScope,
    OriginalImageEvidence,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    EvidenceRef,
    FactRecord,
    FactRef,
    KnownFact,
    ModelInvocationRef,
    ObjectRef,
    PolicyContentRef,
    QuestionFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.values import BooleanValue, validate_typed_value
from jimu_runtime.workflow.engine import DecisionDraft
from jimu_runtime.workflow.mock_context import (
    SyntheticGateResult,
    check_gate_scope,
    literal_applicability,
    snapshot_artifact,
)
from jimu_runtime.workflow.mock_facts import (
    check_object,
    object_artifact,
    producer_catalogue,
)
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticArtifact,
    SyntheticCall,
    SyntheticExecution,
    VerifiedExecutionScope,
    artifact,
    record_digest,
    synthetic_id,
)

type SyntheticAuthority = ObservationBatchSelection | ToolStepSelection


@dataclass(frozen=True, slots=True)
class SyntheticDecisionResult:
    draft: DecisionDraft
    obligations: tuple[MandatoryRuleObligation, ...]
    traces: tuple[ApplicabilityTrace, ...]
    invocation: ModelInvocation | None
    evidence: tuple[EvidenceAsset, ...]
    artifacts: tuple[SyntheticArtifact, ...]

    @property
    def decision(self) -> DecisionRecord:
        return self.draft.decision


def coverage_ref(scope: VerifiedExecutionScope) -> ExecutionCoverageRef:
    return ExecutionCoverageRef(
        record_kind=K.EXECUTION_COVERAGE,
        record_id=synthetic_id("coverage", scope.task.task_id, scope.execution_id),
    )


def _authority_ref(selection: SyntheticAuthority) -> AuthoritySelectionRef:
    return AuthoritySelectionRef(
        record_kind=K.AUTHORITATIVE_RESULT_SELECTION, record_id=selection.selection_id
    )


def _check_authority(fact: FactRecord, selection: SyntheticAuthority) -> bool:
    if (
        fact.execution_id,
        fact.object_id,
        fact.profile_ref,
        fact.attempt_id,
    ) != (
        selection.execution_id,
        selection.object_id,
        selection.profile_ref,
        selection.selected_attempt_id,
    ):
        return False
    source = fact.source
    if isinstance(selection, ObservationBatchSelection):
        return (
            isinstance(source, QuestionFactSource)
            and source.observation_batch_ref == selection.selected_result_ref
        )
    return isinstance(source, ToolFactSource) and (
        source.tool_execution_ref,
        source.tool_plan_ref,
        source.step_id,
    ) == (selection.selected_result_ref, selection.tool_plan_ref, selection.step_id)


def _matches(fact: FactRecord, selector: FactSelector) -> bool:
    if (fact.scope, fact.fact_key, fact.fact_schema_ref) != (
        selector.scope,
        selector.fact_key,
        selector.fact_schema_ref,
    ) or selector.field_path:
        return False
    source = fact.source
    selected = selector.producer_selector
    if isinstance(source, QuestionFactSource):
        return (
            isinstance(selected, QuestionProducerSelector)
            and source.question_ref == selected.question_ref
        )
    return (
        isinstance(source, ToolFactSource)
        and isinstance(selected, ToolProducerSelector)
        and (
            source.tool_plan_ref,
            source.step_id,
            source.tool_ref,
            source.output_name,
        )
        == (
            selected.tool_plan_ref,
            selected.step_id,
            selected.tool_ref,
            selected.output_name,
        )
    )


def _check_fact_declaration(scope: SyntheticExecution, fact: FactRecord) -> None:
    catalogue_ref, catalogue = producer_catalogue(scope)
    (declaration,) = tuple(
        item
        for item in catalogue.declarations
        if item.declaration_id == fact.producer_declaration_id
    )
    if (
        fact.producer_catalogue_ref != catalogue_ref
        or fact.profile_ref is None
        or declaration.profile_ref is None
        or declaration.profile_ref.stable_json() != fact.profile_ref.stable_json()
        or (fact.fact_schema_ref, fact.fact_key, fact.scope, fact.producer_kind)
        != (
            declaration.fact_schema_ref,
            declaration.fact_key,
            declaration.scope,
            declaration.producer_kind,
        )
    ):
        raise ValueError("synthetic Fact must match the pinned Catalogue declaration")
    source, producer = fact.source, declaration.producer
    same_source = (
        isinstance(source, QuestionFactSource)
        and isinstance(producer, QuestionProducer)
        and source.question_ref == producer.question_ref
    ) or (
        isinstance(source, ToolFactSource)
        and isinstance(producer, ToolStepProducer)
        and (source.tool_ref, source.tool_plan_ref, source.step_id, source.output_name)
        == (
            producer.tool_ref,
            producer.tool_plan_ref,
            producer.step_id,
            declaration.output_name,
        )
    )
    if not same_source:
        raise ValueError(
            "synthetic Fact must retain the exact declared producer source"
        )
    if isinstance(fact, KnownFact):
        definition = scope.resolve(fact.fact_schema_ref, FactSchemaDefinition)
        validate_typed_value(definition.type_definition, fact.value)


def _fact_operands(
    scope: SyntheticExecution,
    call: SyntheticCall,
    inputs: DecisionInput,
    facts: tuple[FactRecord, ...],
    authorities: tuple[SyntheticAuthority, ...],
) -> dict[FactRef, tuple[FactRecord, AuthoritySelectionRef]]:
    if (
        tuple(fact.fact_id for fact in facts)
        != tuple(ref.record_id for ref in inputs.fact_refs)
        or tuple(_authority_ref(item) for item in authorities)
        != inputs.authority_selection_refs
    ):
        raise ValueError(
            "synthetic decision inputs require exactly the pinned facts and authority revisions"
        )
    obj = inputs.object_record
    fact_map: dict[FactRef, tuple[FactRecord, AuthoritySelectionRef]] = {}
    owners: dict[FactRef, SyntheticAuthority] = {}
    for selection in authorities:
        if (selection.execution_id, selection.object_id, selection.profile_ref) != (
            scope.execution_id,
            obj.object_id,
            obj.profile_ref,
        ):
            raise ValueError(
                "decision authority belongs to a foreign Object or Execution"
            )
        call.check_upstream(
            artifact(
                K.AUTHORITATIVE_RESULT_SELECTION, selection.selection_id, selection
            )
        )
        for ref in selection.selected_fact_refs:
            if ref in owners:
                raise ValueError(
                    "synthetic Decision requires one selected authority for each Fact"
                )
            owners[ref] = selection
    for fact, ref in zip(facts, inputs.fact_refs, strict=True):
        owner = owners.get(ref)
        if owner is None or not _check_authority(fact, owner):
            raise ValueError(
                "synthetic Fact must resolve to its exact producer attempt and selected result"
            )
        call.check_upstream(artifact(K.FACT, fact.fact_id, fact))
        _check_fact_declaration(scope, fact)
        fact_map[ref] = (fact, _authority_ref(owner))
    return fact_map


def _original_image(
    scope: SyntheticExecution,
    call: SyntheticCall,
    obj: ObjectRecord,
    policy_ref: PolicyContentRef,
) -> EvidenceAsset:
    asset = scope.media
    policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
    original = EvidenceAsset(
        schema_version=VERSION,
        evidence_id=call.id("original-image", obj.object_id, policy_ref.stable_json()),
        evidence_type=EvidenceType.ORIGINAL_IMAGE,
        scope=ObjectEvidenceScope(
            scope="object",
            execution_id=scope.execution_id,
            object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
            profile_ref=obj.profile_ref,
        ),
        storage=asset.storage,
        sha256=asset.media_ref.sha256,
        mime_type=asset.mime_type,
        byte_size=asset.byte_size,
        stage=asset.stage,
        spatial=OriginalImageEvidence(
            kind="original_image",
            original_media_ref=asset.media_ref,
            coordinate_system=obj.coordinate_system,
        ),
        producer_id="fixture.parking",
        producer_version=VERSION,
        access_scope=TaskEvidenceAccess(
            scope="task",
            project_id=scope.task.idempotency_scope.project_id,
            task_id=scope.task.task_id,
        ),
        retention=RetentionPolicyRef(
            evidence_policy_ref=policy_ref, retention_class=policy.retention_class
        ),
        created_at=asset.stage.staged_at,
    )
    original.validate_original_media(asset)
    return original


def _condition(
    scope: SyntheticExecution,
    call: SyntheticCall,
    obj: ObjectRecord,
    rule: RuleDefinition,
    fact_map: dict[FactRef, tuple[FactRecord, AuthoritySelectionRef]],
) -> tuple[EvaluatedRuleOutcome | BlockedRuleOutcome, EvidenceAsset | None]:
    expr = rule.condition_ast
    if (
        not isinstance(expr, AllExpr)
        or rule.optional_fact_selectors
        or not all(isinstance(item, FactRefExpr) for item in expr.arguments)
        or {item.selector for item in expr.arguments if isinstance(item, FactRefExpr)}
        != set(rule.required_fact_selectors)
    ):
        raise ValueError(
            "synthetic parking rule requires a conjunction of exactly its required Boolean Facts"
        )
    proofs: list[RequiredFactProof] = []
    values: list[bool] = []
    for selector in rule.required_fact_selectors:
        matched = tuple(
            (ref, fact, sel)
            for ref, (fact, sel) in fact_map.items()
            if _matches(fact, selector)
        )
        if len(matched) > 1:
            raise ValueError("duplicate selected Fact operand")
        if matched:
            ref, fact, sel = matched[0]
            if isinstance(fact, KnownFact):
                if not isinstance(fact.value, BooleanValue):
                    raise ValueError("synthetic Rule requires Boolean Fact values")
                proofs.append(
                    RequiredFactProof(
                        selector=selector, fact_ref=ref, authority_selection_ref=sel
                    )
                )
                values.append(fact.value.value)
    policy_ref = PolicyContentRef.model_validate(rule.evidence_policy_ref.model_dump())
    evidence_policy = scope.resolve(policy_ref, EvidencePolicyArtifact).policy
    needed = set(evidence_policy.required_evidence_types)
    if evidence_policy.original_media_required:
        needed.add(EvidenceType.ORIGINAL_IMAGE)
    original = (
        _original_image(scope, call, obj, policy_ref)
        if EvidenceType.ORIGINAL_IMAGE in needed
        else None
    )
    evidence_refs = (
        ()
        if original is None
        else (EvidenceRef(record_kind=K.EVIDENCE, record_id=original.evidence_id),)
    )
    missing_evidence = needed - {EvidenceType.ORIGINAL_IMAGE}
    reasons = []
    if len(proofs) != len(rule.required_fact_selectors):
        reasons.append(RequiredEvidenceReason.REQUIRED_FACT_UNAVAILABLE)
    if missing_evidence:
        reasons.append(RequiredEvidenceReason.EVIDENCE_UNAVAILABLE)
    fields = {
        "required_fact_refs": tuple(proof.fact_ref for proof in proofs),
        "required_evidence_refs": evidence_refs,
        "authoritative_result_selection_refs": tuple(
            dict.fromkeys(proof.authority_selection_ref for proof in proofs)
        ),
        "fact_proofs": tuple(proofs),
    }
    if reasons:
        return BlockedRuleOutcome(
            outcome="blocked",
            guard=BlockedRequiredEvidenceGuard.model_validate(
                {**fields, "outcome": "blocked", "reasons": tuple(reasons)}
            ),
        ), original
    matched_all = all(values)
    return EvaluatedRuleOutcome(
        outcome="evaluated",
        predicate=PredicateRecord(
            truth=RuleTruth.TRUE if matched_all else RuleTruth.FALSE,
            diagnostic_codes=(),
        ),
        guard=CompleteRequiredEvidenceGuard.model_validate(
            {**fields, "outcome": "complete"}
        ),
        matched=matched_all,
    ), original


def mandatory_paths(
    scope: VerifiedExecutionScope, obj: ObjectRecord
) -> tuple[RulePath, ...]:
    profile = scope.resolve(obj.profile_ref, ProfileDefinition)
    paths = []
    for ref in profile.rule_set_refs:
        rule_set = scope.resolve(ref, RuleSetDefinition)
        for rule_ref in rule_set.rule_refs:
            rule = scope.resolve(rule_ref, RuleDefinition)
            if scope.resolve(rule.rule_policy_ref, RulePolicyArtifact).policy.mandatory:
                paths.append(RulePath(rule_set_ref=ref, rule_ref=rule_ref))
    return tuple(paths)


def stage_decision(
    scope: SyntheticExecution,
    inputs: DecisionInput,
    snapshot: ContextSnapshot,
    gate: SyntheticGateResult,
    at: datetime,
    *,
    facts: tuple[FactRecord, ...],
    authorities: tuple[SyntheticAuthority, ...],
) -> SyntheticDecisionResult:
    inputs = DecisionInput.model_validate(inputs)
    call = scope.check_call(inputs.envelope, NodeId.N06, at)
    scope.check_snapshot(snapshot, inputs.context_snapshot_ref)
    check_gate_scope(scope, gate.evaluation)
    inputs.check_gate(gate.evaluation)
    obj = inputs.object_record
    paths = mandatory_paths(scope, obj)
    inputs.check_mandatory_scope(paths)
    profile = check_object(scope, obj)
    if gate.scope.context_snapshot_digest != record_digest(snapshot):
        raise ValueError(
            "synthetic Decision must inherit the selected eligibility Snapshot"
        )
    call.check_upstream(
        object_artifact(obj), snapshot_artifact(snapshot), gate.artifacts[0]
    )
    fact_map = _fact_operands(scope, call, inputs, facts, authorities)
    (profile_trace,) = tuple(
        item for item in gate.traces if item.profile_ref == obj.profile_ref
    )
    if (
        profile_trace.trace_ref not in gate.evaluation.applicability_traces
        or profile_trace.effective_truth is not RuleTruth.TRUE
    ):
        raise ValueError(
            "Decision must retain this Object's exact eligible Profile trace"
        )
    rules_by_set = {
        ref: scope.resolve(ref, RuleSetDefinition) for ref in profile.rule_set_refs
    }
    candidate_refs = {item.candidate_binding_ref for item in rules_by_set.values()}
    if len(candidate_refs) != 1 or None in candidate_refs:
        raise ValueError(
            "synthetic parking RuleSets require a shared advisory fixture binding"
        )
    (candidate_binding,) = tuple(ref for ref in candidate_refs if ref is not None)
    object_ref = ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id)
    invocation = scope.model_invocation(
        call,
        "N06",
        ModelCapability.CANDIDATE_PREDICTION,
        candidate_binding,
        (obj.profile_ref,),
        record_digest(inputs),
        scope.response_digest(candidate_binding, {"categories": []}),
        object_ref=object_ref,
    )
    execution_pin = {
        "schema_version": VERSION,
        "execution_id": scope.execution_id,
        "release_id": scope.pins.release_id,
        "bundle_digest": scope.pins.bundle_digest,
        "context_snapshot_ref": inputs.context_snapshot_ref,
    }
    object_pin = {
        **execution_pin,
        "profile_ref": obj.profile_ref,
        "object_ref": object_ref,
        "node_run_ref": call.node_run_ref,
        "attempt_ref": call.attempt_ref,
    }
    candidate = CandidatePrediction.model_validate(
        {
            **object_pin,
            "scope": FactScope.OBJECT,
            "prediction_id": call.id("candidate", obj.object_id),
            "status": CandidatePredictionStatus.SUCCEEDED,
            "categories": (),
            "model_invocation_ref": ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
            ),
            "advisory_only": True,
            "created_at": at,
        }
    )
    decision_ref = DecisionRef(
        record_kind=K.DECISION, record_id=call.id("decision", obj.object_id)
    )
    traces: list[ApplicabilityTrace] = []
    rule_sets = []
    rules = []
    obligations: list[MandatoryRuleObligation] = []
    evaluations = []
    matched = {}
    evidence: dict[str, EvidenceAsset] = {}
    conflict_policies = []
    for set_ref, rule_set in rules_by_set.items():
        set_paths = tuple(path for path in paths if path.rule_set_ref == set_ref)
        scope.resolve(rule_set.conflict_policy_ref, ConflictPolicyArtifact)
        conflict_policies.append(
            PolicyContentRef.model_validate(rule_set.conflict_policy_ref.model_dump())
        )
        set_eval_id = call.id("ruleset", obj.object_id, set_ref.stable_json())
        set_ref_runtime = RuleSetApplicabilityEvaluationRef(
            record_kind=K.RULE_SET_APPLICABILITY_EVALUATION, record_id=set_eval_id
        )
        set_level = literal_applicability(
            scope,
            PolicyContentRef.model_validate(
                rule_set.applicability_policy_ref.model_dump()
            ),
            set_ref,
            ApplicabilityLevel.RULE_SET,
            set_ref_runtime,
        )
        if set_level.truth is not RuleTruth.TRUE:
            raise ValueError(
                "fixture N06 supports reached RuleSets using a literal true applicability policy"
            )
        set_trace = ApplicabilityTrace.model_validate(
            {
                **execution_pin,
                "profile_ref": obj.profile_ref,
                "applicability_trace_id": call.id("ruleset-trace", set_eval_id),
                "target_ref": set_ref,
                "levels": (*profile_trace.levels, set_level),
                "context_snapshot_digest": gate.scope.context_snapshot_digest,
                "effective_truth": RuleTruth.TRUE,
                "disposition": ApplicabilityDisposition.INCLUDED,
                "decisive_level": ApplicabilityLevel.RULE_SET,
                "created_at": at,
            }
        )
        traces.append(set_trace)
        set_evaluation = RuleSetApplicabilityEvaluation.model_validate(
            {
                **object_pin,
                "evaluation_id": set_eval_id,
                "profile_eligibility_evaluation_ref": gate.evaluation.evaluation_ref,
                "rule_set_ref": set_ref,
                "applicability_trace_ref": set_trace.trace_ref,
                "recorded_at": at,
            }
        )
        rule_sets.append(set_evaluation)
        obligations.append(
            MandatoryRuleObligation(
                obligation_id=call.id("ruleset-obligation", set_eval_id),
                profile_ref=obj.profile_ref,
                target=RuleSetObligationTarget(
                    scope="rule_set", rule_set_ref=set_ref, object_ref=object_ref
                ),
                outcome=IncludedObligation(
                    disposition="included",
                    selected_applicability=SelectedApplicability(
                        evaluation_ref=set_ref_runtime, trace_ref=set_trace.trace_ref
                    ),
                ),
            )
        )
        for path in set_paths:
            rule = scope.resolve(path.rule_ref, RuleDefinition)
            rule_eval_id = call.id("rule", set_eval_id, path.rule_ref.stable_json())
            rule_ref_runtime = RuleApplicabilityEvaluationRef(
                record_kind=K.RULE_APPLICABILITY_EVALUATION, record_id=rule_eval_id
            )
            level = literal_applicability(
                scope,
                PolicyContentRef.model_validate(
                    rule.applicability_policy_ref.model_dump()
                ),
                path.rule_ref,
                ApplicabilityLevel.RULE,
                rule_ref_runtime,
            )
            trace = ApplicabilityTrace.model_validate(
                {
                    **execution_pin,
                    "profile_ref": obj.profile_ref,
                    "applicability_trace_id": call.id("rule-trace", rule_eval_id),
                    "target_ref": path.rule_ref,
                    "levels": (*set_trace.levels, level),
                    "context_snapshot_digest": gate.scope.context_snapshot_digest,
                    "effective_truth": level.truth,
                    "disposition": ApplicabilityDisposition.INCLUDED
                    if level.truth is RuleTruth.TRUE
                    else ApplicabilityDisposition.EXCLUDED,
                    "decisive_level": ApplicabilityLevel.RULE,
                    "created_at": at,
                }
            )
            traces.append(trace)
            rules.append(
                RuleApplicabilityEvaluation.model_validate(
                    {
                        **object_pin,
                        "evaluation_id": rule_eval_id,
                        "profile_eligibility_evaluation_ref": gate.evaluation.evaluation_ref,
                        "rule_set_ref": set_ref,
                        "rule_ref": path.rule_ref,
                        "rule_set_applicability_evaluation_ref": set_ref_runtime,
                        "applicability_trace_ref": trace.trace_ref,
                        "recorded_at": at,
                    }
                )
            )
            selected = SelectedApplicability(
                evaluation_ref=rule_ref_runtime, trace_ref=trace.trace_ref
            )
            result: EvaluatedRuleOutcome | BlockedRuleOutcome | SkippedRuleOutcome
            outcome: EvaluatedObligation | GuardBlockedObligation | ExcludedObligation
            fact_refs: tuple[FactRef, ...] = ()
            evidence_refs: tuple[EvidenceRef, ...] = ()
            if level.truth is RuleTruth.FALSE:
                result = SkippedRuleOutcome(
                    outcome="skipped", reason=RuleSkipReason.APPLICABILITY_EXCLUDED
                )
                outcome = ExcludedObligation(
                    disposition="excluded", selected_applicability=selected
                )
            else:
                result, original = _condition(scope, call, obj, rule, fact_map)
                if original is not None:
                    evidence[original.evidence_id] = original
                fact_refs = result.guard.required_fact_refs
                evidence_refs = result.guard.required_evidence_refs
                if isinstance(result, BlockedRuleOutcome):
                    outcome = GuardBlockedObligation(
                        disposition="blocked",
                        selected_applicability=selected,
                        decision_ref=decision_ref,
                    )
                else:
                    if result.matched:
                        matched[path] = rule.violation_category
                    outcome = EvaluatedObligation(
                        disposition="evaluated",
                        selected_applicability=selected,
                        decision_ref=decision_ref,
                    )
            evaluations.append(
                RuleEvaluation(
                    rule=path,
                    selected_rule_set_evaluation_ref=set_ref_runtime,
                    selected_rule_evaluation_ref=rule_ref_runtime,
                    applicability_trace_ref=trace.trace_ref,
                    result=result,
                    fact_refs=fact_refs,
                    evidence_refs=evidence_refs,
                )
            )
            obligations.append(
                MandatoryRuleObligation(
                    obligation_id=call.id("rule-obligation", rule_eval_id),
                    profile_ref=obj.profile_ref,
                    target=RuleObligationTarget(
                        scope="rule",
                        object_ref=object_ref,
                        rule_set_ref=set_ref,
                        rule_ref=path.rule_ref,
                    ),
                    outcome=outcome,
                )
            )
    blocked = tuple(
        item.rule for item in evaluations if isinstance(item.result, BlockedRuleOutcome)
    )
    evaluated = tuple(
        item.rule
        for item in evaluations
        if isinstance(item.result, EvaluatedRuleOutcome)
    )
    skipped = tuple(
        item.rule for item in evaluations if isinstance(item.result, SkippedRuleOutcome)
    )
    decision = DecisionRecord.model_validate(
        {
            **object_pin,
            "scope": FactScope.OBJECT,
            "decision_id": decision_ref.record_id,
            "profile_eligibility_evaluation_ref": gate.evaluation.evaluation_ref,
            "candidate_prediction_ref": candidate.prediction_ref,
            "execution_coverage_ref": coverage_ref(scope),
            "rule_engine_version": VERSION,
            "rule_evaluations": tuple(evaluations),
            "result": (
                DecisionResult.VIOLATION
                if matched
                else DecisionResult.UNCERTAIN
                if blocked
                else DecisionResult.NO_VIOLATION
                if evaluated
                else DecisionResult.NOT_APPLICABLE
            ),
            "explanation": ExplanationTrace(
                applicable_rules=evaluated + blocked,
                evaluated_rules=evaluated,
                skipped_rules=skipped,
                blocked_rules=blocked,
                not_evaluated_rules=(),
                applicability_trace_refs=(
                    profile_trace.trace_ref,
                    *(trace.trace_ref for trace in traces),
                ),
                obligation_ids=(
                    synthetic_id(
                        "profile-obligation",
                        scope.task.task_id,
                        scope.execution_id,
                        obj.profile_ref.stable_json(),
                    ),
                    *(item.obligation_id for item in obligations),
                ),
                fact_refs=tuple(
                    dict.fromkeys(ref for item in evaluations for ref in item.fact_refs)
                ),
                evidence_refs=tuple(
                    dict.fromkeys(
                        ref for item in evaluations for ref in item.evidence_refs
                    )
                ),
            ),
            "conflict_resolution": ConflictResolutionTrace(
                mode="collect_all",
                conflict_policy_refs=tuple(dict.fromkeys(conflict_policies)),
                matched_rules=tuple(matched),
                matched_category_ids=tuple(dict.fromkeys(matched.values())),
            ),
            "created_at": at,
        }
    )
    artifacts = (
        artifact(K.DECISION, decision.decision_id, decision),
        artifact(K.CANDIDATE_PREDICTION, candidate.prediction_id, candidate),
        artifact(K.MODEL_INVOCATION, invocation.model_invocation_id, invocation),
        *(
            artifact(K.RULE_SET_APPLICABILITY_EVALUATION, item.evaluation_id, item)
            for item in rule_sets
        ),
        *(
            artifact(K.RULE_APPLICABILITY_EVALUATION, item.evaluation_id, item)
            for item in rules
        ),
        *(
            artifact(K.APPLICABILITY_TRACE, item.applicability_trace_id, item)
            for item in traces
        ),
        *(artifact(K.EVIDENCE, item.evidence_id, item) for item in evidence.values()),
    )
    envelope = call.output(artifacts)
    if not isinstance(envelope, SucceededNodeOutput):
        raise ValueError("synthetic N06 draft must be completed")
    draft = DecisionDraft(
        schema_version=VERSION,
        execution_id=scope.execution_id,
        inputs=inputs,
        output_envelope=envelope,
        decision=decision,
        candidate=candidate,
        rule_set_evaluations=tuple(rule_sets),
        rule_evaluations=tuple(rules),
    )
    return SyntheticDecisionResult(
        draft,
        tuple(obligations),
        tuple(traces),
        invocation,
        tuple(evidence.values()),
        artifacts,
    )


def finish_decision(
    scope: VerifiedExecutionScope,
    staged: SyntheticDecisionResult,
    coverage: ExecutionCoverage,
    gate: SyntheticGateResult,
) -> DecisionOutput:
    coverage = ExecutionCoverage.model_validate(coverage)
    if (
        coverage.coverage_ref != coverage_ref(scope)
        or not set(staged.obligations)
        <= set(coverage.mandatory_rule_coverage.obligations)
        or staged.draft.output_envelope.output_artifacts
        != tuple(item.pin for item in staged.artifacts)
    ):
        raise ValueError(
            "synthetic final Decision requires the exact staged artifacts and shared execution Coverage"
        )
    return staged.draft.bind_coverage(
        coverage,
        gate.evaluation,
        mandatory_paths(scope, staged.draft.inputs.object_record),
        applicability_traces=staged.traces,
    )
