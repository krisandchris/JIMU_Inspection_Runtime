"""Check finite, caller-supplied associations between precomputed records."""

from collections.abc import Callable, Sequence
from typing import Annotated, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    immutable_sequence,
    reference_identity,
    require_unique_references,
)
from jimu_runtime.domain.enums import ArtifactKind, AttemptStatus, NodeRunStatus
from jimu_runtime.domain.policies import ApplicabilityPolicyArtifact
from jimu_runtime.domain.primitives import Digest, RecordId, Version
from jimu_runtime.domain.references import ContentRef, RuntimeRecordRef
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityLevel,
    ApplicabilityTargetRef,
    ApplicabilityTrace,
    ContextSnapshotRef,
    EvaluatedApplicabilityLevel,
    NotEvaluatedApplicabilityLevel,
    ProfileEligibilityEvaluation,
    ProfileEligibilityScopeRefs,
    RuleApplicabilityEvaluation,
    RuleSetApplicabilityEvaluation,
    RuleTruth,
    UnresolvedObligation,
)
from jimu_runtime.domain.runtime.facts import ObjectRef, PolicyContentRef
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeAttemptOwner,
    NodeRunDescriptor,
    ObjectDescriptor,
)

type _Directory[T] = Annotated[
    tuple[T, ...], Field(max_length=4096), BeforeValidator(immutable_sequence)
]
type _Evaluation = RuleSetApplicabilityEvaluation | RuleApplicabilityEvaluation
type _DefinitionKey = tuple[ArtifactKind, str, str, str]


class ApplicabilityTargetBinding(ContractModel):
    """Finite view of one Bundle member's pinned policy and forward membership."""

    target_ref: ApplicabilityTargetRef
    policy_ref: PolicyContentRef
    policy: ApplicabilityPolicyArtifact
    child_refs: _Directory[ApplicabilityTargetRef]

    @model_validator(mode="after")
    def exact_policy_and_child_kinds(self) -> Self:
        if (self.policy_ref.artifact_id, self.policy_ref.version) != (
            self.policy.policy_id,
            self.policy.version,
        ):
            raise ValueError("policy registration identity mismatch")
        require_unique_references(self.child_refs)
        allowed_child_kind = {
            ArtifactKind.PROFILE: ArtifactKind.RULE_SET,
            ArtifactKind.RULE_SET: ArtifactKind.RULE,
        }.get(self.target_ref.artifact_kind)
        if any(
            child.artifact_kind is not allowed_child_kind for child in self.child_refs
        ):
            raise ValueError("forward membership must follow Profile / RuleSet / Rule")
        return self


class ApplicabilityScope(ContractModel):
    """Trusted association inputs for a pinned Execution and its N01 Snapshot."""

    execution_id: RecordId
    release_id: RecordId
    bundle_digest: Digest
    workflow_version: Version
    context_snapshot_ref: ContextSnapshotRef
    context_snapshot_digest: Digest
    context_node_run: NodeRunDescriptor
    configured_profile_refs: ProfileEligibilityScopeRefs
    target_bindings: _Directory[ApplicabilityTargetBinding]

    @model_validator(mode="after")
    def consistent_definition_refs(self) -> Self:
        require_unique_references(self.configured_profile_refs)
        require_unique_references(
            tuple(binding.target_ref for binding in self.target_bindings)
        )
        return self


class ApplicabilityAttemptBinding(ContractModel):
    """Finite view of an Attempt's actual N06 invocation scope."""

    attempt: AttemptDescriptor
    object_ref: ObjectRef | None = None


class ApplicabilityRecordSet(ContractModel):
    """Explicit chosen records for one Execution, with injected association evidence."""

    profile_eligibility: ProfileEligibilityEvaluation
    traces: _Directory[ApplicabilityTrace]
    rule_set_evaluations: _Directory[RuleSetApplicabilityEvaluation]
    rule_evaluations: _Directory[RuleApplicabilityEvaluation]
    unresolved_obligations: _Directory[UnresolvedObligation]
    node_runs: _Directory[NodeRunDescriptor]
    attempts: _Directory[ApplicabilityAttemptBinding]
    objects: _Directory[ObjectDescriptor]


def _runtime_directory[T](
    entries: Sequence[T], reference: Callable[[T], RuntimeRecordRef]
) -> dict[str, T]:
    directory = {reference(entry).record_id: entry for entry in entries}
    if len(directory) != len(entries):
        raise ValueError("duplicate runtime record ID in applicability directory")
    return directory


def _resolve[T](directory: dict[str, T], ref: RuntimeRecordRef) -> T:
    if ref.record_id not in directory:
        raise ValueError(f"missing applicability reference: {ref.record_kind.value}")
    return directory[ref.record_id]


def _same_reference(left: ContentRef, right: ContentRef) -> bool:
    return reference_identity(left) == reference_identity(right)


def _check_pins(
    scope: ApplicabilityScope,
    record: ProfileEligibilityEvaluation | ApplicabilityTrace | _Evaluation,
) -> None:
    if (
        record.execution_id != scope.execution_id
        or record.release_id != scope.release_id
        or record.bundle_digest != scope.bundle_digest
        or record.context_snapshot_ref != scope.context_snapshot_ref
    ):
        raise ValueError(
            "applicability record has mismatched Execution/Release/Bundle/Snapshot pins"
        )


def _check_trace(
    scope: ApplicabilityScope,
    bindings: dict[_DefinitionKey, ApplicabilityTargetBinding],
    trace: ApplicabilityTrace,
) -> None:
    _check_pins(scope, trace)
    if trace.context_snapshot_digest != scope.context_snapshot_digest:
        raise ValueError("applicability trace has a different ContextSnapshot digest")
    parent: ApplicabilityTargetBinding | None = None
    for level in trace.levels:
        key = reference_identity(level.target_ref)
        binding = bindings.get(key)
        if binding is None or not _same_reference(level.policy_ref, binding.policy_ref):
            raise ValueError(
                "applicability target or policy is outside the pinned Bundle view"
            )
        if parent is not None and not any(
            _same_reference(level.target_ref, child) for child in parent.child_refs
        ):
            raise ValueError("applicability path violates forward Bundle membership")
        if isinstance(level, EvaluatedApplicabilityLevel) and not set(
            level.declared_input_refs
        ).issubset(binding.policy.policy.context_allowlist):
            raise ValueError(
                "applicability trace reads metadata outside its policy allowlist"
            )
        parent = binding


def _profile_traces(
    scope: ApplicabilityScope,
    evaluation: ProfileEligibilityEvaluation,
    traces: dict[str, ApplicabilityTrace],
) -> dict[_DefinitionKey, ApplicabilityTrace]:
    _check_pins(scope, evaluation)
    if evaluation.workflow_version != scope.workflow_version:
        raise ValueError("Profile gate must pin the Execution workflow version")
    if evaluation.configured_profile_refs != scope.configured_profile_refs:
        raise ValueError(
            "Profile gate must pin the entire configured Profile scope in order"
        )
    expected = {
        reference_identity(profile): truth
        for truth, profiles in (
            (RuleTruth.TRUE, evaluation.eligible_profiles),
            (RuleTruth.FALSE, evaluation.excluded_profiles),
            (RuleTruth.UNKNOWN, evaluation.unresolved_profiles),
        )
        for profile in profiles
    }
    result: dict[_DefinitionKey, ApplicabilityTrace] = {}
    for ref in evaluation.applicability_traces:
        trace = _resolve(traces, ref)
        profile = reference_identity(trace.profile_ref)
        level = trace.levels[0]
        if (
            len(trace.levels) != 1
            or not isinstance(level, EvaluatedApplicabilityLevel)
            or level.source_evaluation_ref != evaluation.evaluation_ref
            or profile in result
            or expected.get(profile) is not level.truth
        ):
            raise ValueError(
                "Profile traces must match their owning gate and partition exactly"
            )
        result[profile] = trace
    return result


def _check_invocation(
    evaluation: _Evaluation,
    nodes: dict[str, NodeRunDescriptor],
    attempts: dict[str, ApplicabilityAttemptBinding],
) -> None:
    assert evaluation.node_run_ref is not None and evaluation.attempt_ref is not None
    node = _resolve(nodes, evaluation.node_run_ref)
    attempt_binding = _resolve(attempts, evaluation.attempt_ref)
    attempt = attempt_binding.attempt
    if (
        node.execution_id != evaluation.execution_id
        or attempt.execution_id != evaluation.execution_id
        or node.node_id != "N06"
        or node.status
        in (
            NodeRunStatus.PENDING,
            NodeRunStatus.READY,
            NodeRunStatus.BLOCKED,
            NodeRunStatus.SKIPPED,
        )
        or attempt.status is AttemptStatus.CREATED
        or not isinstance(attempt.owner, NodeAttemptOwner)
        or attempt.owner.node_run_ref != evaluation.node_run_ref
        or attempt_binding.object_ref != evaluation.object_ref
    ):
        raise ValueError(
            "evaluated applicability must cite its actual N06 node run and owned attempt"
        )


def _check_evaluation_trace(
    evaluation: _Evaluation,
    trace: ApplicabilityTrace,
    parent_trace: ApplicabilityTrace,
) -> None:
    target_ref = (
        evaluation.rule_ref
        if isinstance(evaluation, RuleApplicabilityEvaluation)
        else evaluation.rule_set_ref
    )
    if (
        not _same_reference(trace.profile_ref, evaluation.profile_ref)
        or not _same_reference(trace.target_ref, target_ref)
        or not _same_reference(trace.levels[1].target_ref, evaluation.rule_set_ref)
        or trace.levels[:-1] != parent_trace.levels
    ):
        raise ValueError(
            "applicability trace must preserve the exact selected parent prefix"
        )
    target_level = trace.levels[-1]
    if isinstance(target_level, EvaluatedApplicabilityLevel):
        if evaluation.decisive_ancestor_ref is not None or (
            target_level.source_evaluation_ref != evaluation.evaluation_ref
        ):
            raise ValueError("evaluated target must identify its owning N06 evaluation")
    elif evaluation.decisive_ancestor_ref != target_level.decisive_ancestor_ref:
        raise ValueError("projected target must share its decisive ancestor")


def _check_object(
    objects: dict[str, ObjectDescriptor],
    owner: _Evaluation | UnresolvedObligation,
) -> None:
    if owner.object_ref is not None:
        item = _resolve(objects, owner.object_ref)
        if item.execution_id != owner.execution_id or not _same_reference(
            item.profile_ref, owner.profile_ref
        ):
            raise ValueError(
                "applicability Object belongs to a different Execution or Profile"
            )


def _check_obligations(
    records: ApplicabilityRecordSet,
    traces: dict[str, ApplicabilityTrace],
    trace_owners: dict[str, _Evaluation],
    objects: dict[str, ObjectDescriptor],
    profile_traces: dict[_DefinitionKey, ApplicabilityTrace],
    rule_sets: dict[str, RuleSetApplicabilityEvaluation],
) -> None:
    by_trace: dict[str, UnresolvedObligation] = {}
    anchor_ids: set[str] = set()
    for item in records.unresolved_obligations:
        trace = _resolve(traces, item.applicability_trace_ref)
        key = trace.applicability_trace_id
        level = trace.levels[-1]
        if (
            key in by_trace
            or trace.effective_truth is not RuleTruth.UNKNOWN
            or item.execution_id != trace.execution_id
            or item.scope is not level.level
            or not _same_reference(item.target_ref, trace.target_ref)
            or not _same_reference(item.profile_ref, trace.profile_ref)
            or item.reason_code is not level.reason_code
            or (
                item.rule_set_ref is not None
                and not _same_reference(item.rule_set_ref, trace.levels[1].target_ref)
            )
        ):
            raise ValueError(
                "unresolved obligation must identify the exact selected UNKNOWN path"
            )
        _check_object(objects, item)
        if isinstance(level, EvaluatedApplicabilityLevel):
            owner = trace_owners.get(key)
            if owner is not None and owner.object_ref != item.object_ref:
                raise ValueError(
                    "direct obligation and N06 evaluation must share Object scope"
                )
            if item.obligation_id in anchor_ids:
                raise ValueError(
                    "one obligation ID cannot count multiple evaluated UNKNOWNs"
                )
            anchor_ids.add(item.obligation_id)
        by_trace[key] = item

    for trace in traces.values():
        level = trace.levels[-1]
        if (
            isinstance(level, EvaluatedApplicabilityLevel)
            and level.truth is RuleTruth.UNKNOWN
        ):
            if trace.applicability_trace_id not in by_trace:
                raise ValueError(
                    "each directly evaluated UNKNOWN requires an unresolved obligation"
                )

    for key, item in by_trace.items():
        trace = traces[key]
        level = trace.levels[-1]
        anchor_trace = trace
        if isinstance(level, NotEvaluatedApplicabilityLevel):
            if item.decisive_ancestor_ref != level.decisive_ancestor_ref:
                raise ValueError(
                    "unresolved projection must cite the same decisive ancestor"
                )
            parent_index = (
                0 if trace.decisive_level is ApplicabilityLevel.PROFILE else 1
            )
            parent = trace.levels[parent_index]
            assert isinstance(parent, EvaluatedApplicabilityLevel)
            if parent_index == 0:
                anchor_trace = profile_traces[reference_identity(trace.profile_ref)]
            else:
                parent_eval = _resolve(rule_sets, parent.source_evaluation_ref)
                anchor_trace = _resolve(traces, parent_eval.applicability_trace_ref)
            if (
                by_trace[anchor_trace.applicability_trace_id].obligation_id
                != item.obligation_id
            ):
                raise ValueError(
                    "unresolved projection must share the ancestor obligation ID"
                )
        anchor_level = anchor_trace.levels[-1]
        assert isinstance(anchor_level, EvaluatedApplicabilityLevel)
        for diagnostic in item.diagnostic_refs:
            if (
                diagnostic.trace_ref != anchor_trace.trace_ref
                or diagnostic.level is not anchor_level.level
                or diagnostic.issue_index >= len(anchor_level.validation_issues)
            ):
                raise ValueError(
                    "unresolved diagnostic must resolve to the decisive UNKNOWN metadata issue"
                )


def validate_applicability_records(
    scope: ApplicabilityScope, records: ApplicabilityRecordSet
) -> None:
    """Reject inconsistent associations in a finite record set without running a policy."""
    n01 = scope.context_node_run
    if (
        n01.execution_id != scope.execution_id
        or n01.node_id != "N01"
        or n01.status is not NodeRunStatus.SUCCEEDED
    ):
        raise ValueError(
            "Profile eligibility requires successful N01 for this Execution"
        )

    bindings = {
        reference_identity(binding.target_ref): binding
        for binding in scope.target_bindings
    }
    traces = _runtime_directory(records.traces, lambda trace: trace.trace_ref)
    nodes = _runtime_directory(records.node_runs, lambda node: node.node_run_ref)
    attempts = _runtime_directory(
        records.attempts, lambda entry: entry.attempt.attempt_ref
    )
    objects = _runtime_directory(records.objects, lambda item: item.object_ref)
    rule_sets = _runtime_directory(
        records.rule_set_evaluations, lambda item: item.evaluation_ref
    )
    rules = _runtime_directory(
        records.rule_evaluations, lambda item: item.evaluation_ref
    )
    for trace in records.traces:
        _check_trace(scope, bindings, trace)
    profile_traces = _profile_traces(scope, records.profile_eligibility, traces)
    trace_owners: dict[str, _Evaluation] = {}
    evaluated_attempt_targets: set[tuple[str, tuple[_DefinitionKey, ...]]] = set()
    selected: tuple[_Evaluation, ...] = (*rule_sets.values(), *rules.values())
    for evaluation in selected:
        _check_pins(scope, evaluation)
        if (
            evaluation.profile_eligibility_evaluation_ref
            != records.profile_eligibility.evaluation_ref
        ):
            raise ValueError(
                "N06 applicability must pin the selected Profile eligibility evaluation"
            )
        profile_trace = profile_traces.get(reference_identity(evaluation.profile_ref))
        if profile_trace is None:
            raise ValueError(
                "N06 applicability Profile must be in the configured gate scope"
            )
        trace = _resolve(traces, evaluation.applicability_trace_ref)
        if trace.applicability_trace_id in trace_owners:
            raise ValueError(
                "one trace cannot belong to multiple N06 evaluation records"
            )
        parent_trace = profile_trace
        if isinstance(evaluation, RuleApplicabilityEvaluation):
            parent = _resolve(
                rule_sets, evaluation.rule_set_applicability_evaluation_ref
            )
            if not _same_reference(parent.rule_set_ref, evaluation.rule_set_ref) or (
                evaluation.decisive_ancestor_ref is None
                and parent.object_ref is not None
                and parent.object_ref != evaluation.object_ref
            ):
                raise ValueError(
                    "Rule must pin the exact parent RuleSet and inherit compatible Object scope"
                )
            parent_trace = _resolve(traces, parent.applicability_trace_ref)
        _check_evaluation_trace(evaluation, trace, parent_trace)
        if evaluation.decisive_ancestor_ref is None:
            assert evaluation.attempt_ref is not None
            attempt_target = (
                evaluation.attempt_ref.record_id,
                tuple(reference_identity(level.target_ref) for level in trace.levels),
            )
            if attempt_target in evaluated_attempt_targets:
                raise ValueError(
                    "one Attempt cannot author multiple evaluations of a target path"
                )
            evaluated_attempt_targets.add(attempt_target)
            _check_invocation(evaluation, nodes, attempts)
            _check_object(objects, evaluation)
        trace_owners[trace.applicability_trace_id] = evaluation

    used = {
        item.applicability_trace_id for item in profile_traces.values()
    } | trace_owners.keys()
    if used != traces.keys():
        raise ValueError(
            "all traces must belong to the selected Profile or N06 evaluations"
        )
    _check_obligations(
        records, traces, trace_owners, objects, profile_traces, rule_sets
    )
