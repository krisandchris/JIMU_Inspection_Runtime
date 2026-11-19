"""Plan the six-node workflow from one durable, execution-scoped snapshot."""

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import require_unique_references
from jimu_runtime.domain.definitions.capabilities import (
    ModelGroundingBinding,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.models import PolicyContentRef
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import ExecutionStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_selectors import ToolPlanContentRef
from jimu_runtime.domain.nodes.context import ContextBuildOutput, SuccessfulContextBuild
from jimu_runtime.domain.nodes.decision import DecisionInput, DecisionOutput
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryOutput,
    SkippedDiscoveryOutput,
)
from jimu_runtime.domain.nodes.envelopes import (
    ArtifactOutputRef,
    NodeOutputEnvelope,
    SucceededNodeOutput,
    check_envelope_pair,
)
from jimu_runtime.domain.nodes.grounding import (
    GroundingOutput,
    GroundingProfileInput,
    _GroundingInvocationGap,
)
from jimu_runtime.domain.policies import ToolSkipPolicy
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityTrace,
    ProfileEligibilityEvaluation,
    RuleApplicabilityEvaluation,
    RuleSetApplicabilityEvaluation,
    UnresolvedObligation,
)
from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    CoverageReason,
    EvaluatedCoverageScope,
    ExecutionCoverage,
)
from jimu_runtime.domain.runtime.decisions import (
    CandidatePrediction,
    DecisionRecord,
    RecordList,
    RulePath,
)
from jimu_runtime.domain.runtime.executions import (
    ContextSnapshotPin,
    ExecutionRecord,
    NodeId,
    NodeRun,
)
from jimu_runtime.domain.runtime.facts import (
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.perception import (
    DiscoveryOutcome,
    GroundingOutcome,
    ObjectRecord,
    ProfileGroundingResult,
)
from jimu_runtime.domain.validation.decision_coverage import ProfileRuleScope
from jimu_runtime.workflow.tool_dependencies import needs_observation

_MAX_PROFILES = 1024
_MAX_OBJECTS = 1024
_OBJECT_NODES = (NodeId.N04, NodeId.N05, NodeId.N06)
_UNSETTLED = (NodeRunStatus.PENDING, NodeRunStatus.READY, NodeRunStatus.RUNNING)
_ACTIVE_EXECUTION = (ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING)
_DISCOVERY_INTERRUPTION_CODES = ("attempt_deadline_elapsed", "lease_expired")


@dataclass(frozen=True, slots=True)
class BranchKey:
    node_id: NodeId
    object_id: RecordId | None = None

    def __post_init__(self) -> None:
        if self.object_id is not None and self.node_id not in _OBJECT_NODES:
            raise ValueError("only N04, N05 and N06 can have Object branches")


@dataclass(frozen=True, slots=True)
class BranchRun:
    key: BranchKey
    run: NodeRun


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    definition: ProfileDefinition
    rules: ProfileRuleScope

    def __post_init__(self) -> None:
        ref = self.rules.profile_ref
        if (ref.artifact_id, ref.version) != (
            self.definition.profile_id,
            self.definition.version,
        ) or tuple(item.rule_set_ref for item in self.rules.rule_sets) != (
            self.definition.rule_set_refs
        ):
            raise ValueError("Profile planning must use the pinned RuleSet membership")

    @property
    def mandatory_rule_paths(self) -> tuple[RulePath, ...]:
        paths = tuple(
            RulePath(rule_set_ref=rule_set.rule_set_ref, rule_ref=rule.rule_ref)
            for rule_set in self.rules.rule_sets
            for rule in rule_set.rules
            if rule.rule_policy.policy.mandatory
        )
        if len(paths) > 1024:
            raise ValueError("Profile mandatory Rule scope exceeds the N06 input limit")
        return paths


class DecisionDraft(ContractModel):
    """Fenced N06 work, pending validation against the shared final ledger."""

    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    inputs: DecisionInput
    output_envelope: SucceededNodeOutput
    decision: DecisionRecord
    candidate: CandidatePrediction | None
    rule_set_evaluations: RecordList[RuleSetApplicabilityEvaluation]
    rule_evaluations: RecordList[RuleApplicabilityEvaluation]

    @model_validator(mode="after")
    def check_invocation(self) -> Self:
        env, obj, decision = (
            self.inputs.envelope,
            self.inputs.object_record,
            self.decision,
        )
        check_envelope_pair(env, self.output_envelope)
        if (
            self.execution_id,
            decision.execution_id,
            decision.release_id,
            decision.bundle_digest,
            decision.context_snapshot_ref,
            decision.profile_eligibility_evaluation_ref,
            decision.profile_ref,
            decision.object_ref.record_id,
            decision.node_run_ref.record_id,
            decision.attempt_ref.record_id,
        ) != (
            env.execution_id,
            env.execution_id,
            env.release_id,
            env.bundle_digest,
            self.inputs.context_snapshot_ref,
            self.inputs.profile_eligibility_evaluation_ref,
            obj.profile_ref,
            obj.object_id,
            env.node_run_id,
            env.attempt_id,
        ):
            raise ValueError(
                "Decision draft must retain its exact invocation and Object"
            )
        if self.output_envelope.trace != env.trace or not any(
            artifact.ref.record_kind is RuntimeRecordKind.DECISION
            and artifact.ref.record_id == decision.decision_id
            for artifact in self.output_envelope.output_artifacts
        ):
            raise ValueError("N06 envelope must pin its Decision and exact trace")
        candidate = self.candidate
        if (candidate is None) != (decision.candidate_prediction_ref is None) or (
            candidate is not None
            and (
                candidate.execution_id,
                candidate.release_id,
                candidate.bundle_digest,
                candidate.context_snapshot_ref,
                candidate.profile_ref,
                candidate.object_ref,
                candidate.node_run_ref,
                candidate.attempt_ref,
                candidate.prediction_ref,
            )
            != (
                decision.execution_id,
                decision.release_id,
                decision.bundle_digest,
                decision.context_snapshot_ref,
                decision.profile_ref,
                decision.object_ref,
                decision.node_run_ref,
                decision.attempt_ref,
                decision.candidate_prediction_ref,
            )
        ):
            raise ValueError(
                "Advisory Candidate must match this Decision draft invocation"
            )
        selected: tuple[
            RuleSetApplicabilityEvaluation | RuleApplicabilityEvaluation, ...
        ] = (*self.rule_set_evaluations, *self.rule_evaluations)
        if len({item.evaluation_ref for item in selected}) != len(selected) or any(
            (
                item.execution_id,
                item.release_id,
                item.bundle_digest,
                item.context_snapshot_ref,
                item.profile_ref,
                item.profile_eligibility_evaluation_ref,
            )
            != (
                decision.execution_id,
                decision.release_id,
                decision.bundle_digest,
                decision.context_snapshot_ref,
                decision.profile_ref,
                decision.profile_eligibility_evaluation_ref,
            )
            or item.node_run_ref is not None
            and (
                item.node_run_ref,
                item.attempt_ref,
                item.object_ref,
            )
            != (
                decision.node_run_ref,
                decision.attempt_ref,
                decision.object_ref,
            )
            for item in selected
        ):
            raise ValueError(
                "Decision draft applicability revisions must match its pinned scope"
            )
        return self

    def bind_coverage(
        self,
        coverage: ExecutionCoverage,
        gate: ProfileEligibilityEvaluation,
        configured_paths: tuple[RulePath, ...],
        *,
        applicability_traces: tuple[ApplicabilityTrace, ...] = (),
    ) -> DecisionOutput:
        values = dict(
            schema_version="1.0.0",
            envelope=self.output_envelope,
            decision_ref=self.decision.decision_ref,
            execution_coverage_ref=coverage.coverage_ref,
            mandatory_rule_coverage=coverage.mandatory_rule_coverage,
        )
        if self.candidate is not None:
            values["candidate_prediction_ref"] = self.candidate.prediction_ref
        output = DecisionOutput.model_validate(values)
        output.check_records(
            self.inputs,
            self.decision,
            coverage,
            self.candidate,
            gate,
            self.rule_set_evaluations,
            self.rule_evaluations,
            configured_paths,
            applicability_traces=applicability_traces,
        )
        return output


class ExecutionFinalization(ContractModel):
    """The shared Coverage and exact Decision set published atomically."""

    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    coverage: ExecutionCoverage
    decisions: RecordList[DecisionRecord]
    projected_traces: RecordList[ApplicabilityTrace] = ()
    projected_rule_set_evaluations: RecordList[RuleSetApplicabilityEvaluation] = ()
    projected_rule_evaluations: RecordList[RuleApplicabilityEvaluation] = ()
    unresolved_obligations: RecordList[UnresolvedObligation] = ()

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        coverage = self.coverage
        object_profiles = {
            ref: profile.profile_ref
            for profile in coverage.profiles
            for ref in profile.object_refs
        }
        if (
            self.execution_id != coverage.execution_id
            or len({decision.object_ref for decision in self.decisions})
            != len(self.decisions)
            or len({decision.decision_id for decision in self.decisions})
            != len(self.decisions)
            or any(
                (
                    decision.execution_id,
                    decision.release_id,
                    decision.bundle_digest,
                    decision.execution_coverage_ref,
                    object_profiles.get(decision.object_ref),
                )
                != (
                    self.execution_id,
                    coverage.release_id,
                    coverage.bundle_digest,
                    coverage.coverage_ref,
                    decision.profile_ref,
                )
                for decision in self.decisions
            )
        ):
            raise ValueError(
                "finalization Decisions must belong to the shared Execution and Coverage"
            )
        self._check_projection_records()
        return self

    def _check_projection_records(self) -> None:
        coverage = self.coverage
        scope = coverage.applicability_scope
        evaluations: tuple[
            RuleSetApplicabilityEvaluation | RuleApplicabilityEvaluation, ...
        ] = (*self.projected_rule_set_evaluations, *self.projected_rule_evaluations)
        selected_projections = {
            (
                entry.outcome.selected_applicability.evaluation_ref,
                entry.outcome.selected_applicability.trace_ref,
            )
            for entry in coverage.mandatory_rule_coverage.obligations
            if isinstance(entry.outcome, AncestorNotEvaluatedObligation)
            and entry.outcome.decisive_ancestor_ref.record_kind
            is RuntimeRecordKind.PROFILE_ELIGIBILITY_EVALUATION
        }
        if selected_projections != {
            (item.evaluation_ref, item.applicability_trace_ref) for item in evaluations
        }:
            raise ValueError(
                "finalization must retain every selected Profile ancestor projection"
            )
        projected = (*self.projected_traces, *evaluations)
        if projected:
            if not isinstance(scope, EvaluatedCoverageScope):
                raise ValueError(
                    "projection records require evaluated Profile eligibility"
                )
            profiles = set(scope.configured_profile_refs)
            expected = (
                self.execution_id,
                coverage.release_id,
                coverage.bundle_digest,
                scope.context_snapshot_ref,
            )
            if any(
                (
                    item.execution_id,
                    item.release_id,
                    item.bundle_digest,
                    item.context_snapshot_ref,
                )
                != expected
                or item.profile_ref not in profiles
                for item in projected
            ) or any(
                item.decisive_ancestor_ref != scope.profile_eligibility_evaluation_ref
                or item.profile_eligibility_evaluation_ref
                != scope.profile_eligibility_evaluation_ref
                for item in evaluations
            ):
                raise ValueError(
                    "projection records must retain the shared eligibility scope"
                )
            traces = {item.trace_ref for item in self.projected_traces}
            refs = {item.evaluation_ref for item in evaluations}
            if (
                len(traces) != len(self.projected_traces)
                or len(refs) != len(evaluations)
                or traces != {item.applicability_trace_ref for item in evaluations}
                or not traces <= set(coverage.applicability_trace_refs)
                or not refs <= set(coverage.selected_applicability_evaluation_refs)
            ):
                raise ValueError(
                    "projected applicability records must match selected Coverage"
                )
        configured_profiles = set(scope.configured_profile_refs)
        unresolved = {item.obligation_ref for item in self.unresolved_obligations}
        if (
            len(unresolved) != len(self.unresolved_obligations)
            or unresolved != set(coverage.unresolved_obligation_refs)
            or any(
                item.execution_id != self.execution_id
                or item.profile_ref not in configured_profiles
                for item in self.unresolved_obligations
            )
        ):
            raise ValueError(
                "unresolved applicability records must match selected Coverage"
            )


@dataclass(frozen=True, slots=True)
class PlannerState:
    """Resolved pinned config and authority-selected records from one transaction.

    The caller resolves definitions from admitted Bundle bytes and validates node
    artifacts with the public Domain relation checkers before selecting them.
    """

    execution: ExecutionRecord
    configured_bundle_digest: Digest
    profiles: tuple[ProfilePlan, ...]
    runs: tuple[BranchRun, ...] = ()
    context_output: ContextBuildOutput | None = None
    eligibility: ProfileEligibilityEvaluation | None = None
    discovery_output: DiscoveryOutput | None = None
    grounding_output: GroundingOutput | None = None
    objects: tuple[ObjectRecord, ...] = ()
    decision_drafts: tuple[DecisionDraft, ...] = ()
    grounding_gaps: tuple[_GroundingInvocationGap, ...] = ()
    tool_plans: tuple[tuple[ToolPlanContentRef, ToolPlanDefinition], ...] = ()
    tool_skip_policies: tuple[tuple[PolicyContentRef, ToolSkipPolicy], ...] = ()


@dataclass(frozen=True, slots=True)
class ContextWork:
    task_id: RecordId


@dataclass(frozen=True, slots=True)
class EligibilityWork:
    context: ContextSnapshotPin
    configured_profiles: tuple[ProfileContentRef, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryWork:
    context: ContextSnapshotPin
    eligibility: ProfileEligibilityEvaluation


@dataclass(frozen=True, slots=True)
class GroundingWork:
    profiles: tuple[GroundingProfileInput, ...]
    discovery_node_run_ref: NodeRunRef


@dataclass(frozen=True, slots=True)
class ObservationWork:
    object_record: ObjectRecord
    object_pin: ArtifactOutputRef


@dataclass(frozen=True, slots=True)
class ToolWork:
    object_record: ObjectRecord
    object_pin: ArtifactOutputRef
    tool_plan_ref: ToolPlanContentRef
    observation_node: NodeRun | None
    needs_observation: bool = True


@dataclass(frozen=True, slots=True)
class DecisionWork:
    object_record: ObjectRecord
    object_pin: ArtifactOutputRef
    mandatory_rule_paths: tuple[RulePath, ...]
    observation_node: NodeRun
    tool_node: NodeRun


type NodeWork = (
    ContextWork
    | DiscoveryWork
    | GroundingWork
    | ObservationWork
    | ToolWork
    | DecisionWork
)


@dataclass(frozen=True, slots=True)
class ReadyWork:
    key: BranchKey
    inputs: NodeWork


@dataclass(frozen=True, slots=True)
class UninvokedWork:
    key: BranchKey
    status: Literal[NodeRunStatus.BLOCKED, NodeRunStatus.SKIPPED]
    reason_code: ArtifactId
    blocker_node_run_ref: NodeRunRef | None


@dataclass(frozen=True, slots=True)
class ProfileCoverageSource:
    profile_ref: ProfileContentRef
    grounding_outcome: GroundingOutcome
    object_refs: tuple[ObjectRef, ...]
    reasons: tuple[CoverageReason, ...]


@dataclass(frozen=True, slots=True)
class ObjectCoverageSource:
    object_ref: ObjectRef
    observation: NodeRun | None
    tools: NodeRun | None
    decision: NodeRun | None
    decision_draft: DecisionDraft | None
    reasons: tuple[CoverageReason, ...]


@dataclass(frozen=True, slots=True)
class CoverageWork:
    context: ContextBuildOutput | None
    eligibility: ProfileEligibilityEvaluation | None
    discovery: DiscoveryOutput | None
    grounding: GroundingOutput | None
    context_node: NodeRun | None
    discovery_node: NodeRun | None
    grounding_node: NodeRun | None
    discovery_outcome: DiscoveryOutcome
    profiles: tuple[ProfileCoverageSource, ...]
    objects: tuple[ObjectCoverageSource, ...]
    reasons: tuple[CoverageReason, ...]
    ready_to_finalize: bool


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    eligibility: EligibilityWork | None
    ready: tuple[ReadyWork, ...]
    uninvoked: tuple[UninvokedWork, ...]
    coverage: CoverageWork


def _runtime_ref_key(ref: RuntimeRecordRef) -> tuple[RuntimeRecordKind, RecordId]:
    return ref.record_kind, ref.record_id


def _node_ref(run: NodeRun) -> NodeRunRef:
    return NodeRunRef(record_kind=RuntimeRecordKind.NODE_RUN, record_id=run.node_run_id)


def _object_ref(obj: ObjectRecord) -> ObjectRef:
    return ObjectRef(record_kind=RuntimeRecordKind.OBJECT, record_id=obj.object_id)


def _settled(run: NodeRun | None) -> bool:
    return run is not None and run.status not in _UNSETTLED


class _Planner:
    def __init__(self, state: PlannerState) -> None:
        self.state = state
        self.tool_plans = dict(state.tool_plans)
        self.tool_skip_policies = dict(state.tool_skip_policies)
        self.profiles = {entry.rules.profile_ref: entry for entry in state.profiles}
        self.runs: dict[BranchKey, NodeRun] = {}
        self.ready: list[ReadyWork] = []
        self.uninvoked: list[UninvokedWork] = []
        self.eligibility: EligibilityWork | None = None
        self.reasons: list[CoverageReason] = []
        self.profile_results: dict[ProfileContentRef, ProfileGroundingResult] = {}
        self.object_coverage: list[ObjectCoverageSource] = []
        self.drafts = {
            draft.decision.object_ref.record_id: draft
            for draft in state.decision_drafts
        }
        self.expected: set[BranchKey] = set()
        self._check_scope()

    def _check_scope(self) -> None:
        state, execution = self.state, self.state.execution
        if state.configured_bundle_digest != execution.pins.bundle_digest:
            raise ValueError("workflow configuration must come from the pinned Bundle")
        if (
            len(self.profiles) != len(state.profiles)
            or len(self.profiles) > _MAX_PROFILES
            or len(state.objects) > _MAX_OBJECTS
            or len(self.drafts) != len(state.decision_drafts)
            or len(self.drafts) > _MAX_OBJECTS
            or len(state.runs) > 6 + 3 * _MAX_OBJECTS
        ):
            raise ValueError("workflow planning requires unique, bounded branch inputs")
        require_unique_references(tuple(self.profiles))
        if len({run.run.node_run_id for run in state.runs}) != len(state.runs):
            raise ValueError("a NodeRun cannot represent multiple logical branches")
        for entry in state.runs:
            key, run = entry.key, entry.run
            branch_objects = tuple(
                item.ref.record_id
                for item in run.upstream_inputs
                if item.ref.record_kind is RuntimeRecordKind.OBJECT
            )
            if (
                key in self.runs
                or run.execution_id != execution.execution_id
                or run.node_id is not key.node_id
                or run.trace.trace_id != execution.trace.trace_id
                or branch_objects != (() if key.object_id is None else (key.object_id,))
            ):
                raise ValueError("NodeRun must match the selected Execution and branch")
            self.runs[key] = run

    def _run(
        self, node_id: NodeId, object_id: RecordId | None = None
    ) -> NodeRun | None:
        key = BranchKey(node_id, object_id)
        self.expected.add(key)
        return self.runs.get(key)

    def _ready(self, key: BranchKey, inputs: NodeWork) -> None:
        run = self.runs.get(key)
        if run is None or run.status is NodeRunStatus.PENDING:
            self.ready.append(ReadyWork(key, inputs))

    def _no_invocation(
        self,
        node_id: NodeId,
        status: Literal[NodeRunStatus.SKIPPED, NodeRunStatus.BLOCKED],
        reason: CoverageReason,
        blocker: NodeRun | None,
    ) -> None:
        run = self._run(node_id)
        if run is None or run.status in (NodeRunStatus.PENDING, NodeRunStatus.READY):
            self.uninvoked.append(
                UninvokedWork(
                    BranchKey(node_id),
                    status,
                    reason.value,
                    _node_ref(blocker) if blocker is not None else None,
                )
            )
        elif run.status is not NodeRunStatus.CANCELLED and (
            run.status is not status
            or run.reason is None
            or run.reason.code != reason.value
        ):
            raise ValueError(
                "an uninvoked branch must retain its planned status and reason"
            )

    def _check_envelope(self, run: NodeRun | None, out: NodeOutputEnvelope) -> None:
        if (
            run is None
            or (out.node_id, out.node_run_id, out.status, out.trace)
            != (run.node_id, run.node_run_id, run.status, run.trace)
            or not run.attempt_history
            or out.attempt_id != run.attempt_history[-1].attempt_ref.record_id
            or not {_runtime_ref_key(item.ref) for item in out.output_artifacts}
            <= {_runtime_ref_key(ref) for ref in run.output_refs}
        ):
            raise ValueError(
                "selected artifacts must match the terminal NodeRun and Attempt"
            )

    def _context(self) -> ContextSnapshotPin | None:
        run = self._run(NodeId.N01)
        output, pin = self.state.context_output, self.state.execution.context_snapshot
        if output is not None:
            self._check_envelope(run, output.envelope)
        if isinstance(output, SuccessfulContextBuild):
            if pin != ContextSnapshotPin(
                snapshot_ref=output.context_snapshot_ref,
                snapshot_digest=output.context_snapshot_digest,
            ):
                raise ValueError(
                    "selected N01 output must match the Execution snapshot pin"
                )
            return pin
        if pin is not None or (
            run is not None and run.status is NodeRunStatus.SUCCEEDED
        ):
            raise ValueError("successful N01 must retain its selected ContextSnapshot")
        if _settled(run):
            self.reasons.append(CoverageReason.CONTEXT_BUILD_BLOCKED)
            for node in (NodeId.N02, NodeId.N03, *_OBJECT_NODES):
                self._no_invocation(
                    node,
                    NodeRunStatus.BLOCKED,
                    CoverageReason.CONTEXT_BUILD_BLOCKED,
                    run,
                )
        else:
            self._ready(
                BranchKey(NodeId.N01), ContextWork(self.state.execution.task_id)
            )
        return None

    def _gate(self, context: ContextSnapshotPin) -> ProfileEligibilityEvaluation | None:
        gate = self.state.eligibility
        configured = tuple(self.profiles)
        if gate is None:
            self.eligibility = EligibilityWork(context, configured)
            return None
        execution = self.state.execution
        if (
            gate.execution_id,
            gate.release_id,
            gate.bundle_digest,
            gate.workflow_version,
            gate.context_snapshot_ref,
            gate.configured_profile_refs,
        ) != (
            execution.execution_id,
            execution.pins.release_id,
            execution.pins.bundle_digest,
            execution.pins.workflow_version,
            context.snapshot_ref,
            configured,
        ):
            raise ValueError(
                "eligibility must retain the pinned ContextSnapshot and Profile scope"
            )
        if gate.unresolved_profiles:
            self.reasons.append(CoverageReason.APPLICABILITY_UNRESOLVED)
        return gate

    def _discovery(
        self, context: ContextSnapshotPin, gate: ProfileEligibilityEvaluation
    ) -> DiscoveryAttemptOutput | None:
        run = self._run(NodeId.N02)
        output = self.state.discovery_output
        if not gate.eligible_profiles:
            reason = CoverageReason.NO_ELIGIBLE_PROFILES
            self.reasons.append(
                reason
                if gate.configured_profile_refs
                else CoverageReason.EMPTY_CONFIGURED_SCOPE
            )
            self._no_invocation(NodeId.N02, NodeRunStatus.SKIPPED, reason, None)
            if (
                isinstance(output, SkippedDiscoveryOutput)
                and run is not None
                and (
                    run.status is NodeRunStatus.SKIPPED
                    and output.node_run_id == run.node_run_id
                )
            ):
                output.check_gate(gate)
                self._skip_grounding(reason, run)
            elif output is not None or (
                run is not None and run.status is NodeRunStatus.SKIPPED
            ):
                raise ValueError(
                    "empty eligibility must retain its attempt-free N02 skip output"
                )
            elif _settled(run):
                self._skip_grounding(reason, run)
            return None
        if isinstance(output, SkippedDiscoveryOutput):
            raise ValueError("eligible Profiles must be offered to discovery")
        if output is not None:
            self._check_envelope(run, output.envelope)
            if output.execution_id != gate.execution_id or not {
                hit.profile_ref for hit in output.profile_hits
            } <= set(gate.eligible_profiles):
                raise ValueError(
                    "discovery hits must belong to this eligible Profile scope"
                )
            outcome = DiscoveryOutcome(output.outcome)
            outcome_reason = {
                DiscoveryOutcome.FAILED: CoverageReason.DISCOVERY_FAILED,
                DiscoveryOutcome.NO_HITS: CoverageReason.DISCOVERY_NO_HITS,
                DiscoveryOutcome.PARTIAL: CoverageReason.DISCOVERY_PARTIAL,
            }.get(outcome)
            if outcome_reason is not None:
                self.reasons.append(outcome_reason)
            if output.profile_hits:
                return output
            if outcome is DiscoveryOutcome.FAILED:
                self._block_grounding(CoverageReason.DISCOVERY_FAILED, run)
            elif outcome_reason is not None:
                self._skip_grounding(outcome_reason, run)
        elif _settled(run):
            if run is not None and (
                run.status is NodeRunStatus.SUCCEEDED
                or run.status is NodeRunStatus.FAILED
                and (
                    run.reason is None
                    or run.reason.code not in _DISCOVERY_INTERRUPTION_CODES
                )
            ):
                raise ValueError("attempted discovery must retain its typed output")
            self.reasons.append(CoverageReason.DISCOVERY_NOT_EVALUATED)
            self._block_grounding(CoverageReason.DISCOVERY_NOT_EVALUATED, run)
        else:
            self._ready(BranchKey(NodeId.N02), DiscoveryWork(context, gate))
        return None

    def _skip_grounding(self, reason: CoverageReason, blocker: NodeRun | None) -> None:
        self._no_invocation(NodeId.N03, NodeRunStatus.SKIPPED, reason, blocker)
        self._skip_objects(NodeRunStatus.SKIPPED, reason, blocker)

    def _block_grounding(self, reason: CoverageReason, blocker: NodeRun | None) -> None:
        self._no_invocation(NodeId.N03, NodeRunStatus.BLOCKED, reason, blocker)
        self._skip_objects(NodeRunStatus.BLOCKED, reason, blocker)

    def _skip_objects(
        self,
        status: Literal[NodeRunStatus.SKIPPED, NodeRunStatus.BLOCKED],
        reason: CoverageReason,
        blocker: NodeRun | None,
    ) -> None:
        for node in _OBJECT_NODES:
            self._no_invocation(node, status, reason, blocker)

    def _ground(
        self, discovery: DiscoveryAttemptOutput
    ) -> tuple[tuple[ObjectRecord, ArtifactOutputRef], ...]:
        run = self._run(NodeId.N03)
        output = self.state.grounding_output
        if output is None:
            if self.state.grounding_gaps:
                raise ValueError("N03 gaps require the selected N03 envelope")
            if run is not None and run.status is NodeRunStatus.SUCCEEDED:
                raise ValueError("successful N03 requires its selected Profile outputs")
            if _settled(run):
                reason = CoverageReason.GROUNDING_NOT_EVALUATED
                self.reasons.append(reason)
                self._skip_objects(NodeRunStatus.BLOCKED, reason, run)
            else:
                self._ready(
                    BranchKey(NodeId.N03),
                    GroundingWork(
                        profiles=tuple(
                            GroundingProfileInput(
                                hit=hit,
                                grounding_binding=self.profiles[
                                    hit.profile_ref
                                ].definition.grounding_binding,
                            )
                            for hit in discovery.profile_hits
                        ),
                        discovery_node_run_ref=NodeRunRef(
                            record_kind=RuntimeRecordKind.NODE_RUN,
                            record_id=discovery.envelope.node_run_id,
                        ),
                    ),
                )
            return ()
        self._check_envelope(run, output.envelope)
        gaps = self.state.grounding_gaps
        missed = {item.profile_ref for item in gaps}
        hits = {item.profile_ref for item in discovery.profile_hits}
        if (
            len(missed) != len(gaps)
            or len({item.dispatch_id for item in gaps}) != len(gaps)
            or not missed <= hits
            or any(
                (item.execution_id, item.node_run_id, item.attempt_id)
                != (
                    self.state.execution.execution_id,
                    output.envelope.node_run_id,
                    output.envelope.attempt_id,
                )
                or not isinstance(
                    binding := self.profiles[
                        item.profile_ref
                    ].definition.grounding_binding,
                    ModelGroundingBinding,
                )
                or binding.model_binding_ref != item.binding_ref
                for item in gaps
            )
        ):
            raise ValueError(
                "N03 gaps must pin the exact Profile binding and selected Attempt"
            )
        if tuple(
            (
                result.profile_ref,
                result.question_selection_ref,
                result.effective_question_refs,
            )
            for result in output.profile_results
        ) != tuple(
            (hit.profile_ref, hit.question_selection_ref, hit.effective_question_refs)
            for hit in discovery.profile_hits
            if hit.profile_ref not in missed
        ):
            raise ValueError(
                "N03 results and gaps must exhaust hits and preserve effective Questions"
            )
        object_pins = {
            item.ref.record_id: item
            for item in output.envelope.output_artifacts
            if item.ref.record_kind is RuntimeRecordKind.OBJECT
        }
        objects = {obj.object_id: obj for obj in self.state.objects}
        expected = {
            ref.record_id
            for result in output.profile_results
            for ref in result.object_refs
        }
        if (
            len(objects) != len(self.state.objects)
            or objects.keys() != expected
            or object_pins.keys() != expected
        ):
            raise ValueError(
                "N03 Object records and pins must exhaust selected grounding"
            )
        if expected and output.envelope.status is not NodeRunStatus.SUCCEEDED:
            raise ValueError(
                "only successful or partial usable grounding can release Objects"
            )
        branches: list[tuple[ObjectRecord, ArtifactOutputRef]] = []
        for result in output.profile_results:
            self.profile_results[result.profile_ref] = result
            for ref in result.object_refs:
                obj = objects[ref.record_id]
                if (
                    obj.execution_id,
                    obj.profile_ref,
                    obj.grounding_record_ref,
                    obj.question_selection_ref,
                    obj.effective_question_refs,
                    obj.entity_ref,
                ) != (
                    self.state.execution.execution_id,
                    result.profile_ref,
                    result.grounding_record_ref,
                    result.question_selection_ref,
                    result.effective_question_refs,
                    self.profiles[result.profile_ref].definition.entity_ref,
                ):
                    raise ValueError(
                        "Object must inherit the selected Profile grounding and Questions"
                    )
                branches.append((obj, object_pins[obj.object_id]))
        if not branches:
            for outcome, reason in (
                (GroundingOutcome.FAILED, CoverageReason.GROUNDING_FAILED),
                (GroundingOutcome.PARTIAL, CoverageReason.GROUNDING_PARTIAL),
                (GroundingOutcome.NO_TARGET, CoverageReason.GROUNDING_NO_TARGET),
            ):
                if any(item.outcome is outcome for item in output.profile_results):
                    self._skip_objects(
                        NodeRunStatus.SKIPPED
                        if outcome is GroundingOutcome.NO_TARGET
                        else NodeRunStatus.BLOCKED,
                        reason,
                        run,
                    )
                    break
        return tuple(branches)

    def _tool_work(
        self, obj: ObjectRecord, pin: ArtifactOutputRef, observation: NodeRun | None
    ) -> ToolWork | None:
        plan_ref = self.profiles[obj.profile_ref].definition.tool_plan_ref
        plan = self.tool_plans.get(plan_ref)
        if plan is None:
            needs_batch = True
            starts_without_batch = False
        else:
            fact_steps = {
                step.step_id
                for step in plan.steps
                if needs_observation(
                    step, obj, self.tool_skip_policies.get(step.skip_policy_ref)
                )
            }
            needs_batch = bool(fact_steps)
            starts_without_batch = not needs_batch or any(
                not step.depends_on and step.step_id not in fact_steps
                for step in plan.steps
            )
        observation_prepared = observation if _settled(observation) else None
        if starts_without_batch or observation_prepared is not None:
            return ToolWork(obj, pin, plan_ref, observation_prepared, needs_batch)
        return None

    def _object(self, obj: ObjectRecord, pin: ArtifactOutputRef) -> None:
        key = obj.object_id
        observation = self._run(NodeId.N04, key)
        tools = decision = None
        reasons: list[CoverageReason] = []
        if not _settled(observation):
            self._ready(BranchKey(NodeId.N04, key), ObservationWork(obj, pin))
        tool_work = self._tool_work(obj, pin, observation)
        if tool_work is not None:
            tools = self._run(NodeId.N05, key)
            if not _settled(tools):
                self._ready(BranchKey(NodeId.N05, key), tool_work)
        if (
            observation is not None
            and _settled(observation)
            and tools is not None
            and _settled(tools)
        ):
            decision = self._run(NodeId.N06, key)
            if (
                observation.status is not NodeRunStatus.SUCCEEDED
                or tools.status is not NodeRunStatus.SUCCEEDED
            ):
                reasons.append(CoverageReason.REQUIRED_EVIDENCE_BLOCKED)
            self._ready(
                BranchKey(NodeId.N06, key),
                DecisionWork(
                    obj,
                    pin,
                    self.profiles[obj.profile_ref].mandatory_rule_paths,
                    observation,
                    tools,
                ),
            )
            if (
                _settled(decision)
                and decision is not None
                and decision.status is not NodeRunStatus.SUCCEEDED
            ):
                reasons.append(CoverageReason.DECISION_NOT_EVALUATED)
        for run in (observation, tools, decision):
            if run is not None and not any(
                (_runtime_ref_key(item.ref), item.digest)
                == (_runtime_ref_key(pin.ref), pin.digest)
                for item in run.upstream_inputs
            ):
                raise ValueError(
                    "Object branch must pin the authoritative N03 artifact"
                )
        draft = self.drafts.get(key)
        if draft is not None:
            self._check_draft(draft, obj, decision)
        elif (
            decision is not None
            and decision.status is NodeRunStatus.SUCCEEDED
            and self.state.execution.status in _ACTIVE_EXECUTION
        ):
            raise ValueError("successful N06 must retain its selected Decision draft")
        self.object_coverage.append(
            ObjectCoverageSource(
                _object_ref(obj), observation, tools, decision, draft, tuple(reasons)
            )
        )

    def _check_draft(
        self, draft: DecisionDraft, obj: ObjectRecord, run: NodeRun | None
    ) -> None:
        gate = self.state.eligibility
        if (
            gate is None
            or run is None
            or run.status not in (NodeRunStatus.RUNNING, NodeRunStatus.SUCCEEDED)
            or not run.attempt_history
            or draft.inputs.object_record != obj
            or draft.inputs.envelope.node_run_id != run.node_run_id
            or draft.inputs.envelope.attempt_id
            != run.attempt_history[-1].attempt_ref.record_id
            or draft.inputs.envelope.trace != run.trace
            or run.status is NodeRunStatus.SUCCEEDED
            and _runtime_ref_key(draft.decision.decision_ref)
            not in {_runtime_ref_key(ref) for ref in run.output_refs}
        ):
            raise ValueError(
                "Decision draft must belong to the current Object and N06 Attempt"
            )
        paths = self.profiles[obj.profile_ref].mandatory_rule_paths
        draft.inputs.check_gate(gate)
        draft.inputs.check_mandatory_scope(paths)
        explanation = draft.decision.explanation
        accounted = (
            explanation.evaluated_rules
            + explanation.blocked_rules
            + explanation.skipped_rules
            + explanation.not_evaluated_rules
        )
        if not set(paths) <= set(accounted):
            raise ValueError(
                "Decision draft must retain every configured mandatory Rule"
            )

    def plan(self) -> WorkflowPlan:
        state = self.state
        context = self._context()
        gate = self._gate(context) if context is not None else None
        discovery = (
            self._discovery(context, gate)
            if context is not None and gate is not None
            else None
        )
        branches = self._ground(discovery) if discovery is not None else ()
        for obj, pin in branches:
            self._object(obj, pin)
        if (
            state.eligibility is not None
            and gate is None
            or state.discovery_output is not None
            and gate is None
            or state.grounding_output is not None
            and discovery is None
            or state.objects
            and discovery is None
            or state.objects
            and state.grounding_output is None
            or self.runs.keys() - self.expected
            or self.drafts.keys() - {obj.object_id for obj, _ in branches}
        ):
            raise ValueError(
                "persisted workflow branches require their selected predecessors"
            )
        if (
            len(
                {
                    draft.decision.execution_coverage_ref
                    for draft in self.drafts.values()
                }
            )
            > 1
        ):
            raise ValueError(
                "Decision drafts must share one execution-wide Coverage reference"
            )

        profiles = self._profile_sources(gate)
        reasons = tuple(
            dict.fromkeys(
                [
                    *self.reasons,
                    *(reason for profile in profiles for reason in profile.reasons),
                    *(reason for obj in self.object_coverage for reason in obj.reasons),
                ]
            )
        )
        # Drafts are durability- and fence-checked by the caller. A running N06
        # with a validated draft can enter the atomic authority publication step.
        draft_keys = {BranchKey(NodeId.N06, obj_id) for obj_id in self.drafts}
        ready_to_finalize = self.eligibility is None and all(
            _settled(self.runs.get(key)) or key in draft_keys for key in self.expected
        )
        if state.execution.status not in _ACTIVE_EXECUTION:
            self.eligibility = None
            self.ready.clear()
            self.uninvoked.clear()
            ready_to_finalize = False
        return WorkflowPlan(
            self.eligibility,
            tuple(self.ready),
            tuple(self.uninvoked),
            CoverageWork(
                state.context_output,
                state.eligibility,
                state.discovery_output,
                state.grounding_output,
                self.runs.get(BranchKey(NodeId.N01)),
                self.runs.get(BranchKey(NodeId.N02)),
                self.runs.get(BranchKey(NodeId.N03)),
                DiscoveryOutcome(state.discovery_output.outcome)
                if state.discovery_output is not None
                else DiscoveryOutcome.NOT_EVALUATED,
                profiles,
                tuple(self.object_coverage),
                reasons,
                ready_to_finalize,
            ),
        )

    def _profile_sources(
        self, gate: ProfileEligibilityEvaluation | None
    ) -> tuple[ProfileCoverageSource, ...]:
        discovery = self.state.discovery_output
        hits = (
            {hit.profile_ref for hit in discovery.profile_hits}
            if isinstance(discovery, DiscoveryAttemptOutput)
            else set()
        )
        profiles: list[ProfileCoverageSource] = []
        eligible = set(gate.eligible_profiles) if gate is not None else set()
        unresolved = set(gate.unresolved_profiles) if gate is not None else set()
        missed = eligible - hits
        for ref in self.profiles:
            result = self.profile_results.get(ref)
            outcome = (
                result.outcome if result is not None else GroundingOutcome.NOT_EVALUATED
            )
            reasons: list[CoverageReason] = []
            reason = {
                GroundingOutcome.NO_TARGET: CoverageReason.GROUNDING_NO_TARGET,
                GroundingOutcome.FAILED: CoverageReason.GROUNDING_FAILED,
                GroundingOutcome.PARTIAL: CoverageReason.GROUNDING_PARTIAL,
                GroundingOutcome.NOT_EVALUATED: CoverageReason.GROUNDING_NOT_EVALUATED,
            }.get(outcome)
            if reason is not None:
                reasons.append(reason)
            if gate is None and CoverageReason.CONTEXT_BUILD_BLOCKED in self.reasons:
                reasons.append(CoverageReason.CONTEXT_BUILD_BLOCKED)
            if ref in unresolved:
                reasons.append(CoverageReason.APPLICABILITY_UNRESOLVED)
            if isinstance(discovery, DiscoveryAttemptOutput) and ref in missed:
                reasons.append(CoverageReason.PROFILE_NOT_DISCOVERED)
            profiles.append(
                ProfileCoverageSource(
                    ref, outcome, result.object_refs if result else (), tuple(reasons)
                )
            )
        return tuple(profiles)


def plan_work(state: PlannerState) -> WorkflowPlan:
    """Derive work and coverage inputs without I/O, clocks or new invocation IDs."""
    return _Planner(state).plan()
