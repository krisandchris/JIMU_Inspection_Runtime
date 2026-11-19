"""Build and revalidate an N06 Decision on its fixed producer and evidence inputs."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ModelCapability,
)
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.enums import DecisionResult, NodeRunStatus
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.decision import DecisionInput
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope, SucceededNodeOutput
from jimu_runtime.domain.policies import (
    ConflictPolicyArtifact,
    EvaluatedFallbackPolicy,
    FallbackPolicyArtifact,
    RulePolicyArtifact,
)
from jimu_runtime.domain.rules import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityLevel,
    ApplicabilityLevelTrace,
    ApplicabilityTrace,
    EvaluatedApplicabilityLevel,
    RuleApplicabilityEvaluation,
    RuleApplicabilityEvaluationRef,
    RuleSetApplicabilityEvaluation,
    RuleSetApplicabilityEvaluationRef,
    RuleTruth,
    UnresolvedObligation,
    UnresolvedObligationRef,
)
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    EvaluatedObligation,
    ExcludedObligation,
    GuardBlockedObligation,
    IncludedObligation,
    MandatoryRuleObligation,
    ObligationDisposition,
    RuleObligationTarget,
    RuleSetObligationTarget,
    SelectedApplicability,
    UnresolvedMandatoryObligation,
)
from jimu_runtime.domain.runtime.decisions import (
    BlockedRequiredEvidenceGuard,
    BlockedRuleOutcome,
    CandidatePrediction,
    CandidatePredictionStatus,
    ConflictResolutionTrace,
    DecisionRecord,
    DecisionRef,
    EvaluatedRuleOutcome,
    ExplanationTrace,
    RuleEvaluation,
    RulePath,
    RuleSkipReason,
    SkippedRuleOutcome,
)
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    EvidenceRef,
    FactRecord,
    FactRef,
    ModelInvocationRef,
    ObjectRef,
    PolicyContentRef,
)
from jimu_runtime.domain.runtime.invocations import (
    FallbackProviderRoute,
    ModelInvocation,
)
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.validation.coverage_sources import CoverageAuthorityInput
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleConditionContext,
    RuleSetTypeRegistration,
)
from jimu_runtime.inspection.decision.applicability import (
    ApplicabilityEvaluator,
    MetadataInputs,
    extend_trace,
    project_rule,
    type_bindings,
    unresolved_gate,
)
from jimu_runtime.inspection.perception.candidates import CandidateModelResult
from jimu_runtime.inspection.perception.context import validate_context_projection
from jimu_runtime.inspection.rules._guard_sources import check_input_pin
from jimu_runtime.inspection.rules.evidence_guard import RequiredEvidenceGuardBuilder
from jimu_runtime.inspection.rules.predicates import PreparedPredicate
from jimu_runtime.workflow.engine import DecisionDraft
from jimu_runtime.workflow.mock_decision import SyntheticDecisionResult, coverage_ref
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticCall,
    artifact,
    record_digest,
    synthetic_id,
)
from jimu_runtime.workflow.worker_nodes import BranchInputs, NodeResult
from jimu_runtime.workflow.worker_records import DecisionMessage
from jimu_runtime.workflow.worker_source import OpenExecution


@dataclass(frozen=True, slots=True)
class DecisionSourceSnapshot:
    """Authoritative records and bounded evidence reader for one fenced N06 input."""

    authority: CoverageAuthorityInput
    evidence_refs: tuple[EvidenceRef, ...]
    evidence: tuple[EvidenceAsset, ...]
    read_evidence: Callable[[EvidenceAsset, int], bytes | None]
    max_evidence_bytes: int


def _profile(opened: OpenExecution, obj: ObjectRecord) -> ProfileDefinition:
    return opened.scope.resolve(obj.profile_ref, ProfileDefinition)


def mandatory_paths(opened: OpenExecution, obj: ObjectRecord) -> tuple[RulePath, ...]:
    """Enumerate mandatory paths in admitted membership order."""
    scope = opened.scope
    return tuple(
        RulePath(rule_set_ref=set_ref, rule_ref=rule_ref)
        for set_ref in _profile(opened, obj).rule_set_refs
        for rule_ref in scope.resolve(set_ref, RuleSetDefinition).rule_refs
        if scope.resolve(
            scope.resolve(rule_ref, RuleDefinition).rule_policy_ref, RulePolicyArtifact
        ).policy.mandatory
    )


_AMBIGUOUS_BINDING = "candidate_binding_ambiguous"


def _candidate_bindings(
    opened: OpenExecution, obj: ObjectRecord
) -> set[ModelBindingContentRef]:
    bindings = {}
    for ref in _profile(opened, obj).rule_set_refs:
        binding = opened.scope.resolve(ref, RuleSetDefinition).candidate_binding_ref
        if binding is not None:
            bindings[reference_identity(binding)] = (
                ModelBindingContentRef.model_validate(binding.model_dump())
            )
    return set(bindings.values())


def candidate_binding(
    opened: OpenExecution, obj: ObjectRecord
) -> ModelBindingContentRef | None:
    """Dispatch only when the pinned Profile supplies one distinct advisory binding."""
    refs = _candidate_bindings(opened, obj)
    return next(iter(refs)) if len(refs) == 1 else None


def _check_candidate_deployment(
    opened: OpenExecution, binding: ModelBindingContentRef, invocation: ModelInvocation
) -> None:
    (pin,) = tuple(
        item
        for item in opened.scope.pins.model_pins
        if reference_identity(item.binding_ref) == reference_identity(binding)
    )
    requirements = {item.deployment_id: item for item in pin.deployments}
    primary = requirements[pin.primary_deployment_id]
    config = invocation.configured_provider
    if (
        config.primary_deployment_id,
        config.provider_id,
        config.provider_kind,
        config.logical_model_id,
        reference_identity(config.provider_policy_ref),
    ) != (
        primary.deployment_id,
        primary.provider_id,
        primary.provider_kind,
        primary.logical_model_id,
        reference_identity(pin.provider_policy_ref),
    ):
        raise ValueError(
            "candidate provider differs from its pinned model configuration"
        )
    route = invocation.route_trace
    selected = requirements.get(route.selected_deployment_id)
    if selected is None:
        raise ValueError("candidate deployment is outside the pinned configuration")
    if isinstance(route, FallbackProviderRoute):
        policy = opened.scope.resolve(
            pin.fallback_policy_ref, FallbackPolicyArtifact
        ).policy
        if (
            not isinstance(policy, EvaluatedFallbackPolicy)
            or route.trigger_error not in policy.trigger_errors
            or not set(route.evaluation_refs) <= set(policy.evaluation_refs)
            or not any(
                (item.from_deployment_id, item.to_deployment_id)
                == (route.from_deployment_id, route.to_deployment_id)
                for item in policy.allowed_transitions
            )
        ):
            raise ValueError(
                "candidate fallback is outside the pinned evaluated transitions"
            )
    actual = invocation.actual_provider
    if actual is None:
        return
    if (actual.deployment_id, actual.provider_id, actual.provider_kind) != (
        selected.deployment_id,
        selected.provider_id,
        selected.provider_kind,
    ):
        raise ValueError(
            "candidate invocation belongs to a different provider deployment"
        )
    if invocation.outcome.status == "succeeded" and (
        actual.version_resolution == "unavailable"
        or not any(
            deployment.deployment_id == actual.deployment_id
            and deployment.deployment_version == actual.deployment_version
            and deployment.model_version == actual.model_version
            for allowed in opened.resolved.model_deployments
            if reference_identity(allowed.binding_ref) == reference_identity(binding)
            for deployment in allowed.deployments
        )
    ):
        raise ValueError("candidate response uses an unpinned provider version")


def _candidate_prediction(
    opened: OpenExecution,
    call: SyntheticCall,
    inputs: DecisionInput,
    candidate: CandidateModelResult | None,
) -> CandidatePrediction | None:
    if candidate is None:
        return None
    invocation = candidate.invocation
    obj = inputs.object_record
    binding = candidate_binding(opened, obj)
    if binding is None:
        raise ValueError("unconfigured N06 must not supply a candidate response")
    if invocation is None:
        if candidate.categories or candidate.failure_code in (None, _AMBIGUOUS_BINDING):
            raise ValueError("candidate result requires an attributed invocation")
        return None
    object_ref = ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id)
    if (
        invocation.execution_id,
        invocation.node_id,
        invocation.node_run_ref,
        invocation.attempt_ref,
        invocation.object_ref,
        invocation.profile_refs,
        reference_identity(invocation.configured_provider.binding_ref),
        invocation.purposes,
        invocation.trace_id,
    ) != (
        call.envelope.execution_id,
        "N06",
        call.node_run_ref,
        call.attempt_ref,
        object_ref,
        (obj.profile_ref,),
        reference_identity(binding),
        (ModelCapability.CANDIDATE_PREDICTION,),
        call.envelope.trace.trace_id,
    ) or invocation.started_at < call.at:
        raise ValueError("candidate must belong to the exact configured N06 call")

    _check_candidate_deployment(opened, binding, invocation)
    scope = opened.scope
    allowed = {
        scope.resolve(ref, RuleDefinition).violation_category
        for set_ref in _profile(opened, obj).rule_set_refs
        for ref in scope.resolve(set_ref, RuleSetDefinition).rule_refs
    }
    failure = candidate.failure_code
    if failure is None and (
        invocation.outcome.status != "succeeded"
        or not {item.category_id for item in candidate.categories} <= allowed
    ):
        raise ValueError(
            "successful candidate requires actual registered response categories"
        )
    return CandidatePrediction.model_validate(
        {
            "schema_version": VERSION,
            "execution_id": call.envelope.execution_id,
            "release_id": call.envelope.release_id,
            "bundle_digest": call.envelope.bundle_digest,
            "context_snapshot_ref": inputs.context_snapshot_ref,
            "scope": FactScope.OBJECT,
            "profile_ref": obj.profile_ref,
            "object_ref": object_ref,
            "node_run_ref": call.node_run_ref,
            "attempt_ref": call.attempt_ref,
            "prediction_id": call.id("candidate", obj.object_id),
            "status": CandidatePredictionStatus.FAILED
            if failure is not None
            else CandidatePredictionStatus.SUCCEEDED,
            "categories": candidate.categories,
            "model_invocation_ref": ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
            ),
            "advisory_only": True,
            "created_at": invocation.completed_at,
            **({"failure_code": failure} if failure is not None else {}),
        }
    )


def _new_decision_input(
    opened: OpenExecution,
    env: NodeInputEnvelope,
    obj: ObjectRecord,
    previous: BranchInputs,
    sources: DecisionSourceSnapshot,
) -> DecisionInput:
    gate = previous.gate
    context = previous.context
    if (
        gate is None
        or context is None
        or not isinstance(context.output, SuccessfulContextBuild)
    ):
        raise ValueError("N06 requires the selected successful N01 and Pre-N02 Gate")
    records = sources.authority.records
    if records.eligibility != gate.evaluation:
        raise ValueError("N06 authority snapshot must retain the selected Profile Gate")
    snapshot = previous.snapshot()
    opened.scope.check_snapshot(snapshot, context.output.context_snapshot_ref)
    digest = record_digest(snapshot)
    execution_pin = records.execution.context_snapshot
    if (
        gate.scope.context_snapshot_digest != digest
        or execution_pin is None
        or (execution_pin.snapshot_ref, execution_pin.snapshot_digest)
        != (context.output.context_snapshot_ref, digest)
    ):
        raise ValueError(
            "N06 must inherit the exact Gate and Execution ContextSnapshot"
        )
    pins = {
        (pin.ref.record_kind, pin.ref.record_id): pin for pin in env.upstream_artifacts
    }
    for kind, identity, record in (
        (K.CONTEXT_SNAPSHOT, snapshot.context_snapshot_id, snapshot),
        (
            K.PROFILE_ELIGIBILITY_EVALUATION,
            gate.evaluation.evaluation_id,
            gate.evaluation,
        ),
        (K.OBJECT, obj.object_id, obj),
    ):
        if (kind, identity) not in pins:
            raise ValueError(
                "N06 input is missing its Context, Profile Gate or Object pin"
            )
        check_input_pin(pins, kind, identity, record)
    facts = {fact.fact_id: fact for fact in records.facts}
    selections = {
        record.selection_id: record for record in records.authority_selections
    }
    fact_refs = []
    selection_refs = []
    source_record: FactRecord | AuthoritativeResultSelection | None
    for pin in env.upstream_artifacts:
        kind, identity = pin.ref.record_kind, pin.ref.record_id
        if kind is K.FACT:
            source_record = facts.get(identity)
            if source_record is None:
                raise ValueError(
                    "N06 pinned Fact is absent from the authority snapshot"
                )
            fact_refs.append(FactRef(record_kind=K.FACT, record_id=identity))
        elif kind is K.AUTHORITATIVE_RESULT_SELECTION:
            source_record = selections.get(identity)
            if source_record is None:
                raise ValueError(
                    "N06 pinned authority is absent from the source snapshot"
                )
            selection_refs.append(
                AuthoritySelectionRef(
                    record_kind=K.AUTHORITATIVE_RESULT_SELECTION, record_id=identity
                )
            )
        else:
            continue
        if record.execution_id != env.execution_id or (
            source_record.object_id,
            source_record.profile_ref,
        ) not in ((None, None), (obj.object_id, obj.profile_ref)):
            raise ValueError("N06 pinned Fact or authority belongs to another scope")
        check_input_pin(pins, kind, identity, source_record)
    # Evidence availability is evaluated against all exact N06 Evidence pins.
    if {ref.record_id for ref in sources.evidence_refs} != {
        pin.ref.record_id
        for pin in env.upstream_artifacts
        if pin.ref.record_kind is K.EVIDENCE
    }:
        raise ValueError("N06 requires sources for the exact pinned Evidence set")
    return DecisionInput(
        schema_version=VERSION,
        envelope=env,
        object_record=obj,
        context_snapshot_ref=context.output.context_snapshot_ref,
        profile_eligibility_evaluation_ref=gate.evaluation.evaluation_ref,
        mandatory_rule_paths=mandatory_paths(opened, obj),
        fact_refs=tuple(fact_refs),
        authority_selection_refs=tuple(selection_refs),
    )


@dataclass(slots=True)
class _DecisionScan:
    call: SyntheticCall
    inputs: DecisionInput
    evaluator: ApplicabilityEvaluator
    guards: RequiredEvidenceGuardBuilder
    decision_ref: DecisionRef
    profile_trace: ApplicabilityTrace
    evaluated_at: datetime
    rules: list[RuleApplicabilityEvaluation] = field(default_factory=list)
    groups: list[RuleSetApplicabilityEvaluation] = field(default_factory=list)
    traces: list[ApplicabilityTrace] = field(default_factory=list)
    obligations: list[MandatoryRuleObligation] = field(default_factory=list)
    evaluations: list[RuleEvaluation] = field(default_factory=list)
    not_evaluated: list[RulePath] = field(default_factory=list)
    matched_categories: dict[RulePath, str] = field(default_factory=dict)
    evidence: dict[str, EvidenceAsset] = field(default_factory=dict)
    conflict_policies: list[PolicyContentRef] = field(default_factory=list)
    mandatory_gap: bool = False
    mandatory_evaluated: bool = False

    @property
    def object_ref(self) -> ObjectRef:
        return ObjectRef(
            record_kind=K.OBJECT, record_id=self.inputs.object_record.object_id
        )

    def obligation(
        self,
        target: RuleSetObligationTarget | RuleObligationTarget,
        selected: SelectedApplicability,
        outcome: ObligationDisposition | None,
        truth: RuleTruth,
    ) -> MandatoryRuleObligation:
        env = self.call.envelope
        profile = self.inputs.object_record.profile_ref
        identity = synthetic_id(
            "coverage-obligation",
            env.task_id,
            env.execution_id,
            profile.stable_json(),
            target.stable_json(),
        )
        if outcome is None:
            if truth is RuleTruth.UNKNOWN:
                outcome = UnresolvedMandatoryObligation(
                    disposition="unresolved",
                    selected_applicability=selected,
                    unresolved_obligation_ref=UnresolvedObligationRef(
                        record_kind=K.UNRESOLVED_OBLIGATION, record_id=identity
                    ),
                )
            elif truth is RuleTruth.FALSE:
                outcome = ExcludedObligation(
                    disposition="excluded", selected_applicability=selected
                )
            else:
                outcome = IncludedObligation(
                    disposition="included", selected_applicability=selected
                )
        row = MandatoryRuleObligation(
            obligation_id=identity, profile_ref=profile, target=target, outcome=outcome
        )
        self.obligations.append(row)
        return row

    def scan(self) -> None:
        opened = self.evaluator.opened
        scope = opened.scope
        obj = self.inputs.object_record
        for ref in _profile(opened, obj).rule_set_refs:
            ruleset = scope.resolve(ref, RuleSetDefinition)
            scope.resolve(ruleset.conflict_policy_ref, ConflictPolicyArtifact)
            self.conflict_policies.append(
                PolicyContentRef.model_validate(
                    ruleset.conflict_policy_ref.model_dump()
                )
            )
            identity = self.call.id("ruleset", obj.object_id, ref.stable_json())
            group = RuleSetApplicabilityEvaluationRef(
                record_kind=K.RULE_SET_APPLICABILITY_EVALUATION, record_id=identity
            )
            level = self.evaluator.evaluate(
                obj.profile_ref,
                ref,
                PolicyContentRef.model_validate(
                    ruleset.applicability_policy_ref.model_dump()
                ),
                ApplicabilityLevel.RULE_SET,
                group,
            )
            trace = extend_trace(
                self.profile_trace,
                level,
                self.call.id("ruleset-trace", identity),
                self.evaluated_at,
            )
            self.traces.append(trace)
            base = {
                "schema_version": VERSION,
                "execution_id": trace.execution_id,
                "release_id": trace.release_id,
                "bundle_digest": trace.bundle_digest,
                "context_snapshot_ref": trace.context_snapshot_ref,
                "profile_ref": obj.profile_ref,
                "profile_eligibility_evaluation_ref": self.inputs.profile_eligibility_evaluation_ref,
                "rule_set_ref": ref,
                "recorded_at": self.evaluated_at,
            }
            invocation = {
                "object_ref": self.object_ref,
                "node_run_ref": self.call.node_run_ref,
                "attempt_ref": self.call.attempt_ref,
            }
            self.groups.append(
                RuleSetApplicabilityEvaluation.model_validate(
                    {
                        **base,
                        **invocation,
                        "evaluation_id": identity,
                        "applicability_trace_ref": trace.trace_ref,
                    }
                )
            )
            selected = SelectedApplicability(
                evaluation_ref=group, trace_ref=trace.trace_ref
            )
            root = self.obligation(
                RuleSetObligationTarget(
                    scope="rule_set", object_ref=self.object_ref, rule_set_ref=ref
                ),
                selected,
                None,
                level.truth,
            )
            self.mandatory_gap |= level.truth is RuleTruth.UNKNOWN
            for rule_ref in ruleset.rule_refs:
                self.rule(
                    RulePath(rule_set_ref=ref, rule_ref=rule_ref),
                    ruleset,
                    group,
                    trace,
                    base,
                    invocation,
                    root,
                )

    def rule(
        self,
        path: RulePath,
        ruleset: RuleSetDefinition,
        group: RuleSetApplicabilityEvaluationRef,
        parent: ApplicabilityTrace,
        base: dict[str, object],
        invocation: Mapping[str, object],
        root: MandatoryRuleObligation,
    ) -> None:
        scope = self.evaluator.opened.scope
        rule = scope.resolve(path.rule_ref, RuleDefinition)
        mandatory = path in self.inputs.mandatory_rule_paths
        if not mandatory:
            return
        profile = self.inputs.object_record.profile_ref
        identity = self.call.id("rule", group.record_id, path.rule_ref.stable_json())
        source = RuleApplicabilityEvaluationRef(
            record_kind=K.RULE_APPLICABILITY_EVALUATION, record_id=identity
        )
        policy = PolicyContentRef.model_validate(
            rule.applicability_policy_ref.model_dump()
        )
        level: ApplicabilityLevelTrace
        if parent.effective_truth is RuleTruth.TRUE:
            level = self.evaluator.evaluate(
                profile, path.rule_ref, policy, ApplicabilityLevel.RULE, source
            )
            relation = invocation
        else:
            level = project_rule(parent, path.rule_ref, policy, group)
            relation = {"decisive_ancestor_ref": group}
        trace = extend_trace(
            parent, level, self.call.id("rule-trace", identity), self.evaluated_at
        )
        self.traces.append(trace)
        self.rules.append(
            RuleApplicabilityEvaluation.model_validate(
                {
                    **base,
                    **relation,
                    "evaluation_id": identity,
                    "rule_ref": path.rule_ref,
                    "rule_set_applicability_evaluation_ref": group,
                    "applicability_trace_ref": trace.trace_ref,
                }
            )
        )
        selected = SelectedApplicability(
            evaluation_ref=source, trace_ref=trace.trace_ref
        )
        outcome: ObligationDisposition | None = None
        if not isinstance(level, EvaluatedApplicabilityLevel):
            self.not_evaluated.append(path)
            outcome = AncestorNotEvaluatedObligation(
                disposition="not_evaluated",
                selected_applicability=selected,
                decisive_ancestor_ref=group,
                ancestor_obligation_id=root.obligation_id,
            )
        elif level.truth is RuleTruth.UNKNOWN:
            self.not_evaluated.append(path)
            self.mandatory_gap = True
        elif level.truth is RuleTruth.FALSE:
            self.evaluations.append(
                RuleEvaluation(
                    rule=path,
                    selected_rule_set_evaluation_ref=group,
                    selected_rule_evaluation_ref=source,
                    applicability_trace_ref=trace.trace_ref,
                    result=SkippedRuleOutcome(
                        outcome="skipped",
                        reason=RuleSkipReason.APPLICABILITY_EXCLUDED,
                    ),
                    fact_refs=(),
                    evidence_refs=(),
                )
            )
        else:
            outcome = self.condition(
                path, rule, ruleset, group, source, trace, selected
            )

        self.obligation(
            RuleObligationTarget(
                scope="rule",
                object_ref=self.object_ref,
                rule_set_ref=path.rule_set_ref,
                rule_ref=path.rule_ref,
            ),
            selected,
            outcome,
            trace.effective_truth,
        )

    def condition(
        self,
        path: RulePath,
        rule: RuleDefinition,
        ruleset: RuleSetDefinition,
        group: RuleSetApplicabilityEvaluationRef,
        source: RuleApplicabilityEvaluationRef,
        trace: ApplicabilityTrace,
        selected: SelectedApplicability,
    ) -> EvaluatedObligation | GuardBlockedObligation:
        evaluator = self.evaluator
        opened = evaluator.opened
        obj = self.inputs.object_record
        use = evaluator.use(obj.profile_ref)
        refs = condition_references(rule.condition_ast)
        guarded = self.guards.build(path)
        condition = PreparedPredicate(
            catalogue=opened.scope.catalogue,
            input_domain=ConditionInputDomain.RULE_CONDITION,
            context=RuleConditionContext.model_validate(
                {
                    "use_role": "rule_condition",
                    "profile_ref": obj.profile_ref.model_dump(),
                    "rule_ref": path.rule_ref.model_dump(),
                    "rule_set": RuleSetTypeRegistration(
                        ref=path.rule_set_ref, definition=ruleset
                    ),
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
        predicate = condition.evaluate(
            execution_id=self.inputs.envelope.execution_id,
            object_id=obj.object_id,
            context_fields=evaluator.fields(obj.profile_ref, refs.context_selectors),
            facts=guarded.predicate_facts,
        )
        outcome: EvaluatedObligation | GuardBlockedObligation
        result: EvaluatedRuleOutcome | BlockedRuleOutcome
        if isinstance(guarded.guard, BlockedRequiredEvidenceGuard):
            self.mandatory_gap = True
            result = BlockedRuleOutcome(
                outcome="blocked", guard=guarded.guard, predicate=predicate
            )
            outcome = GuardBlockedObligation(
                disposition="blocked",
                selected_applicability=selected,
                decision_ref=self.decision_ref,
            )
        else:
            self.mandatory_evaluated = True
            self.mandatory_gap |= predicate.truth is RuleTruth.UNKNOWN
            matched = predicate.truth is RuleTruth.TRUE
            if matched:
                self.matched_categories[path] = rule.violation_category
            result = EvaluatedRuleOutcome(
                outcome="evaluated",
                guard=guarded.guard,
                predicate=predicate,
                matched=matched,
            )
            outcome = EvaluatedObligation(
                disposition="evaluated",
                selected_applicability=selected,
                decision_ref=self.decision_ref,
            )
        self.evidence.update((item.evidence_id, item) for item in guarded.evidence)
        self.evaluations.append(
            RuleEvaluation(
                rule=path,
                selected_rule_set_evaluation_ref=group,
                selected_rule_evaluation_ref=source,
                applicability_trace_ref=trace.trace_ref,
                result=result,
                optional_fact_proofs=guarded.optional_fact_proofs,
                fact_refs=guarded.fact_refs,
                evidence_refs=guarded.guard.required_evidence_refs,
            )
        )
        return outcome

    def decision(self, prediction: CandidatePrediction | None) -> DecisionRecord:
        env = self.call.envelope
        profile = self.inputs.object_record.profile_ref
        evaluations = self.evaluations
        evaluated = tuple(
            item.rule
            for item in evaluations
            if isinstance(item.result, EvaluatedRuleOutcome)
        )
        blocked = tuple(
            item.rule
            for item in evaluations
            if isinstance(item.result, BlockedRuleOutcome)
        )
        skipped = tuple(
            item.rule
            for item in evaluations
            if isinstance(item.result, SkippedRuleOutcome)
        )
        applicable = tuple(
            item.rule
            for item in evaluations
            if not (
                isinstance(item.result, SkippedRuleOutcome)
                and item.result.reason is RuleSkipReason.APPLICABILITY_EXCLUDED
            )
        )
        if self.matched_categories:
            result = DecisionResult.VIOLATION
        elif self.mandatory_gap:
            result = DecisionResult.UNCERTAIN
        elif self.mandatory_evaluated:
            result = DecisionResult.NO_VIOLATION
        else:
            result = DecisionResult.NOT_APPLICABLE
        return DecisionRecord.model_validate(
            {
                "schema_version": VERSION,
                "execution_id": env.execution_id,
                "release_id": env.release_id,
                "bundle_digest": env.bundle_digest,
                "context_snapshot_ref": self.inputs.context_snapshot_ref,
                "scope": FactScope.OBJECT,
                "profile_ref": profile,
                "object_ref": self.object_ref,
                "node_run_ref": self.call.node_run_ref,
                "attempt_ref": self.call.attempt_ref,
                "decision_id": self.decision_ref.record_id,
                "profile_eligibility_evaluation_ref": self.inputs.profile_eligibility_evaluation_ref,
                "execution_coverage_ref": coverage_ref(self.evaluator.opened.scope),
                "rule_engine_version": VERSION,
                "rule_evaluations": tuple(evaluations),
                "result": result,
                "explanation": ExplanationTrace(
                    applicable_rules=applicable,
                    evaluated_rules=evaluated,
                    skipped_rules=skipped,
                    blocked_rules=blocked,
                    not_evaluated_rules=tuple(self.not_evaluated),
                    applicability_trace_refs=(
                        self.profile_trace.trace_ref,
                        *(item.trace_ref for item in self.traces),
                    ),
                    obligation_ids=(
                        synthetic_id(
                            "profile-obligation",
                            env.task_id,
                            env.execution_id,
                            profile.stable_json(),
                        ),
                        *(item.obligation_id for item in self.obligations),
                    ),
                    fact_refs=tuple(
                        dict.fromkeys(
                            ref for item in evaluations for ref in item.fact_refs
                        )
                    ),
                    evidence_refs=tuple(
                        dict.fromkeys(
                            ref for item in evaluations for ref in item.evidence_refs
                        )
                    ),
                ),
                "conflict_resolution": ConflictResolutionTrace(
                    mode="collect_all",
                    conflict_policy_refs=tuple(dict.fromkeys(self.conflict_policies)),
                    matched_rules=tuple(self.matched_categories),
                    matched_category_ids=tuple(
                        dict.fromkeys(self.matched_categories.values())
                    ),
                ),
                "created_at": self.evaluated_at,
                **(
                    {"candidate_prediction_ref": prediction.prediction_ref}
                    if prediction is not None
                    else {}
                ),
            }
        )


def build_decision(
    opened: OpenExecution,
    previous: BranchInputs,
    envelope: NodeInputEnvelope,
    object_record: ObjectRecord,
    at: datetime,
    sources: DecisionSourceSnapshot,
    candidate: CandidateModelResult | None,
) -> NodeResult:
    """Stage one N06 result using a validated physical source slice."""
    scope = opened.scope
    call = scope.check_call(envelope, NodeId.N06, at)
    inputs = _new_decision_input(opened, envelope, object_record, previous, sources)
    gate, context = previous.gate, previous.context
    assert (
        gate is not None
        and context is not None
        and isinstance(context.output, SuccessfulContextBuild)
    )
    inputs.check_gate(gate.evaluation)
    validate_context_projection(
        opened, context.inputs, context.output, previous.snapshot()
    )
    (profile_trace,) = tuple(
        ApplicabilityTrace.model_validate(trace)
        for trace in gate.traces
        if trace.profile_ref == object_record.profile_ref
    )
    gate_level = profile_trace.levels[0]
    if (
        profile_trace.trace_ref not in gate.evaluation.applicability_traces
        or profile_trace.effective_truth is not RuleTruth.TRUE
        or (
            profile_trace.execution_id,
            profile_trace.release_id,
            profile_trace.bundle_digest,
            profile_trace.context_snapshot_ref,
            profile_trace.context_snapshot_digest,
            profile_trace.target_ref,
        )
        != (
            envelope.execution_id,
            envelope.release_id,
            envelope.bundle_digest,
            inputs.context_snapshot_ref,
            gate.scope.context_snapshot_digest,
            object_record.profile_ref,
        )
        or not isinstance(gate_level, EvaluatedApplicabilityLevel)
        or gate_level.source_evaluation_ref != gate.evaluation.evaluation_ref
    ):
        raise ValueError(
            "N06 must preserve this Object Profile's selected eligible trace"
        )
    pins = {
        (pin.ref.record_kind, pin.ref.record_id): pin
        for pin in envelope.upstream_artifacts
    }
    trace_id = profile_trace.applicability_trace_id
    if (K.APPLICABILITY_TRACE, trace_id) not in pins:
        raise ValueError("N06 input is missing the exact selected Profile trace pin")
    check_input_pin(pins, K.APPLICABILITY_TRACE, trace_id, profile_trace)

    prediction = _candidate_prediction(opened, call, inputs, candidate)
    invocation = None if candidate is None else candidate.invocation
    candidate_diagnostic = None
    if len(_candidate_bindings(opened, object_record)) > 1:
        candidate_diagnostic = _AMBIGUOUS_BINDING
    elif candidate is not None and invocation is None:
        candidate_diagnostic = candidate.failure_code
    evaluator = ApplicabilityEvaluator(
        opened,
        MetadataInputs(previous.snapshot(), context.output),
        type_bindings(opened),
    )
    guards = RequiredEvidenceGuardBuilder(
        catalogue=scope.catalogue,
        authority=sources.authority,
        inputs=inputs,
        task=scope.task,
        media=scope.media,
        evidence_refs=sources.evidence_refs,
        evidence=sources.evidence,
        read_evidence=sources.read_evidence,
        max_evidence_bytes=sources.max_evidence_bytes,
    )
    scan = _DecisionScan(
        call,
        inputs,
        evaluator,
        guards,
        DecisionRef(
            record_kind=K.DECISION,
            record_id=call.id("decision", object_record.object_id),
        ),
        profile_trace,
        at if prediction is None else prediction.created_at,
    )
    scan.scan()
    decision = scan.decision(prediction)
    artifacts = (
        artifact(K.DECISION, decision.decision_id, decision),
        *(
            ()
            if prediction is None
            else (
                artifact(K.CANDIDATE_PREDICTION, prediction.prediction_id, prediction),
            )
        ),
        *(
            ()
            if invocation is None
            else (
                artifact(
                    K.MODEL_INVOCATION, invocation.model_invocation_id, invocation
                ),
            )
        ),
        *(
            artifact(K.RULE_SET_APPLICABILITY_EVALUATION, item.evaluation_id, item)
            for item in scan.groups
        ),
        *(
            artifact(K.RULE_APPLICABILITY_EVALUATION, item.evaluation_id, item)
            for item in scan.rules
        ),
        *(
            artifact(K.APPLICABILITY_TRACE, item.applicability_trace_id, item)
            for item in scan.traces
        ),
        *(
            artifact(K.EVIDENCE, item.evidence_id, item)
            for item in scan.evidence.values()
        ),
    )
    duration = (decision.created_at - at) // timedelta(milliseconds=1)
    output = SucceededNodeOutput(
        schema_version=VERSION,
        node_id=NodeId.N06,
        node_run_id=envelope.node_run_id,
        attempt_id=envelope.attempt_id,
        trace=envelope.trace,
        status=NodeRunStatus.SUCCEEDED,
        output_artifacts=tuple(item.pin for item in artifacts),
        started_at=at,
        finished_at=decision.created_at,
        duration_ms=duration,
    )
    message = DecisionMessage(
        schema_version=VERSION,
        draft=DecisionDraft(
            schema_version=VERSION,
            execution_id=scope.execution_id,
            inputs=inputs,
            output_envelope=output,
            decision=decision,
            candidate=prediction,
            rule_set_evaluations=tuple(scan.groups),
            rule_evaluations=tuple(scan.rules),
        ),
        obligations=tuple(scan.obligations),
        traces=tuple(scan.traces),
        invocation=invocation,
        evidence=tuple(scan.evidence.values()),
        candidate_diagnostic=candidate_diagnostic,
    )
    return NodeResult(
        message,
        artifacts,
        () if prediction is None else (prediction.model_invocation_ref,),
    )


def validate_decision(
    opened: OpenExecution,
    previous: BranchInputs,
    message: DecisionMessage,
    sources: DecisionSourceSnapshot,
    *,
    envelope: NodeInputEnvelope,
    object_record: ObjectRecord,
    at: datetime,
    candidate: CandidateModelResult | None = None,
) -> None:
    """Recompute from the accepted scheduler call and actual advisory witness."""
    envelope = NodeInputEnvelope.model_validate(envelope)
    if (
        message.draft.inputs.envelope != envelope
        or message.draft.inputs.object_record != object_record
        or message.draft.output_envelope.started_at != at
    ):
        raise ValueError("N06 result belongs to a different claimed invocation")
    if candidate is None and (
        message.draft.candidate is not None or message.invocation is not None
    ):
        raise ValueError(
            "N06 candidate needs its independently verified model response"
        )
    if candidate is None and message.candidate_diagnostic not in (
        None,
        _AMBIGUOUS_BINDING,
    ):
        candidate = CandidateModelResult((), None, message.candidate_diagnostic)
    calculated = build_decision(
        opened, previous, envelope, object_record, at, sources, candidate
    )
    if calculated.message != message:
        raise ValueError(
            "N06 result differs from deterministic evaluation of its pinned sources"
        )


def decision_unresolved(
    message: DecisionMessage | SyntheticDecisionResult,
) -> tuple[UnresolvedObligation, ...]:
    """Resolve references to reached UNKNOWN gates for the execution ledger."""
    traces = {item.trace_ref: item for item in message.traces}
    unresolved = []
    for entry in message.obligations:
        target, outcome = entry.target, entry.outcome
        if not isinstance(outcome, UnresolvedMandatoryObligation):
            continue
        trace = traces[outcome.selected_applicability.trace_ref]
        if (
            not isinstance(target, RuleSetObligationTarget | RuleObligationTarget)
            or target.object_ref is None
        ):
            raise ValueError(
                "N06 unresolved obligation requires its exact Object scope"
            )
        unresolved.append(
            unresolved_gate(
                trace,
                entry.obligation_id,
                target.object_ref,
                target.rule_set_ref
                if isinstance(target, RuleObligationTarget)
                else None,
            )
        )
    return tuple(unresolved)
